"""What an install report says about an ``ai.conductor`` block it was given.

The report goes back to whoever installed the package and is stored with the
install, and the block is text nobody vetted: a map key, an extra field or a
rejected value in it can be a credential of any shape. So a finding names a
place by the namespace's own field names, a list position and the servers the
package's ``mcp.json`` declares under a legal name, and nothing else the package
wrote. These run real packages through ``validate_package``, the path an upload
takes.
"""

from __future__ import annotations

import json
import random
import string
import time

import pytest

from src.server.models.plugin import Diagnostic
from src.server.services.plugins import validate_package
from src.server.services.plugins import extension as extension_module
from src.server.services.plugins import manifest as manifest_module
from src.server.services.plugins.extension import (
    ConductorExtension,
    apply_server_metadata,
    declared_server_names,
    namespace_path,
    parse_extension,
    resolve_binds,
)
from src.server.services.plugins.manifest import validate_manifest
from src.server.services.plugins.mcp import McpEntryPlan
from src.server.services.plugins.package import _inline_icons
from tests.unit.server.mcp_builders import zip_package

PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
NS = "extensions.ai.conductor"

# Shapes a credential comes in: a prefixed token, a dashed one, a passphrase
# with spaces, and a bare hex run. None is recognisable to a pattern scrub as a
# whole, which is why a name must not be repeated on the strength of its shape.
SECRETS = [
    "ghp_" "Zq9XyW4vB7nK2mLpR8sT3uJ6",
    "xoxb-" "4417-8820-ABCDEFGHIJKL",
    "correct horse battery staple 42",
    "9f86d081884c7d659a2feaa0c55ad015",
]


def package(extension, *, mcp_servers=None, manifest=None) -> bytes:
    servers = {
        "api": {"type": "streamable-http", "url": "https://api.example/mcp"},
        "cli": {"type": "stdio", "command": "cli-mcp", "env": {"SHIPPED": "x"}},
    }
    return zip_package({
        "plugin.json": json.dumps({
            "$schema": PLUGIN_SCHEMA,
            "name": "probe",
            "version": "1.0.0",
            "extensions": {"ai.conductor": extension},
            **(manifest or {}),
        }).encode(),
        "mcp.json": json.dumps({
            "$schema": MCP_SCHEMA,
            "mcpServers": servers if mcp_servers is None else mcp_servers,
        }).encode(),
    })


def report(raw: bytes) -> list[Diagnostic]:
    return validate_package(raw).diagnostics


def wire(diagnostics: list[Diagnostic]) -> str:
    """Everything the report carries, as it is returned and stored."""
    return json.dumps([d.model_dump() for d in diagnostics], ensure_ascii=False)


def messages(diagnostics: list[Diagnostic], code: str) -> list[str]:
    return [d.message for d in diagnostics if d.code == code]


@pytest.mark.parametrize("secret", SECRETS)
class TestAMapKeyTheCallerChose:
    def test_a_server_block_keyed_by_a_credential_is_not_repeated(self, secret):
        diagnostics = report(package({
            "servers": {
                "api": {"tool_exposure_mode": "loud"},
                secret: {"tool_exposure_mode": "loud"},
            },
        }))

        assert secret not in wire(diagnostics)
        # The entry that names a server the package declares keeps its name,
        # the one that does not says only that it is a key.
        assert sorted(messages(diagnostics, "extension_invalid")) == [
            f"{NS}.servers.<key>: tool_exposure_mode: "
            "Input should be 'summary' or 'detailed'; ignored",
            f"{NS}.servers.api: tool_exposure_mode: "
            "Input should be 'summary' or 'detailed'; ignored",
        ]

    def test_describing_a_server_nothing_declares_does_not_name_it(self, secret):
        diagnostics = report(package({"servers": {secret: {"description": "d"}}}))

        assert secret not in wire(diagnostics)
        [unknown] = [d for d in diagnostics if d.code == "server_meta_unknown"]
        assert unknown.target == ""
        assert unknown.message.startswith(f"{NS}: describes an unknown server")

    def test_several_of_them_are_one_finding_with_the_count(self, secret):
        diagnostics = report(package({"servers": {
            secret: {"description": "d"},
            secret + "2": {"description": "d"},
            "api": {"description": "d"},
        }}))

        assert secret not in wire(diagnostics)
        assert messages(diagnostics, "server_meta_unknown") == [
            f"{NS}: describes 2 unknown servers (none of the package's mcp.json "
            "entries has those keys)"
        ]

    def test_an_icon_warning_does_not_name_the_server_it_is_under(self, secret):
        diagnostics = report(package({
            # One warning per missing file, so each block names its own.
            "servers": {
                secret: {"icon": "./gone.png"},
                "api": {"icon": "./also-gone.png"},
            },
        }))

        assert secret not in wire(diagnostics)
        assert sorted(d.message.split(":")[0] for d in diagnostics
                      if d.code == "icon_unusable") == [
            f"{NS}.servers.<key>.icon",
            f"{NS}.servers.api.icon",
        ]

    def test_a_namespace_that_is_not_an_object_is_named_only_if_it_is_ours(
        self, secret
    ):
        diagnostics = report(package(
            {},
            manifest={"extensions": {
                secret: "oops", "other.vendor": [1], "ai.conductor": 7
            }},
        ))

        assert secret not in wire(diagnostics)
        assert sorted(d.target for d in diagnostics
                      if d.code == "extensions_invalid") == [
            "extensions.<key>",
            "extensions.<key>",
            "extensions.ai.conductor",
        ]
        # Two that are not ours read alike, so each says its place.
        assert sorted(messages(diagnostics, "extensions_invalid")) == [
            "extensions.<key> (namespace 1 of 3) is not of type 'object'",
            "extensions.<key> (namespace 2 of 3) is not of type 'object'",
            "extensions.ai.conductor is not of type 'object'",
        ]


@pytest.mark.parametrize("secret", SECRETS)
class TestAValueInTheWrongPlace:
    @pytest.mark.parametrize(
        ("extension", "path", "sentence"),
        [
            ({"icon": {"k": "{s}"}}, "icon", "Input should be a valid string"),
            ({"secrets": "{s}"}, "secrets", "Input should be a valid list"),
            (
                {"servers": {"api": {"tool_exposure_mode": "{s}"}}},
                "servers.api",
                "tool_exposure_mode: Input should be 'summary' or 'detailed'",
            ),
            (
                {"secrets": [{"name": "K", "label": "{s}" * 90}]},
                "secrets[0]",
                "label: String should have at most 80 characters",
            ),
            (
                {"secrets": [{"name": "K!{s}", "label": "l"}]},
                "secrets[0]",
                "name: String should match pattern",
            ),
        ],
    )
    def test_the_field_and_the_rule_stay_the_value_does_not(
        self, secret, extension, path, sentence
    ):
        # Pydantic's own sentences never quote the input, so this pins the
        # report's shape and guards a validator that starts to; the cases that
        # do quote it are the pattern test below.
        extension = json.loads(json.dumps(extension).replace("{s}", secret))
        diagnostics = report(package(extension))

        assert secret not in wire(diagnostics)
        assert any(
            m.startswith(f"{NS}.{path}: ") and sentence in m
            for m in messages(diagnostics, "extension_invalid")
        ), messages(diagnostics, "extension_invalid")

    @pytest.mark.parametrize("typo", ["Detailed", "DETAILED", "detailed "])
    def test_a_typo_of_an_allowed_word_still_gets_the_list_it_is_a_typo_of(
        self, secret, typo
    ):
        # The list is the schema's, not the package's, and the typo is the one
        # case in which the cut of the package's own text would take it out.
        diagnostics = report(package({
            "servers": {"api": {"tool_exposure_mode": typo}}
        }))

        assert messages(diagnostics, "extension_invalid") == [
            f"{NS}.servers.api: tool_exposure_mode: "
            "Input should be 'summary' or 'detailed'; ignored"
        ]

    @pytest.mark.parametrize("padding", [0, 3000])
    @pytest.mark.parametrize("group", [SECRETS[0], "wxyzabc"])
    def test_a_pattern_that_does_not_compile_is_reported_by_position_only(
        self, secret, group, padding
    ):
        # ``re`` names the group it could not find, which is a slice of what
        # the package wrote: no field name or rule says that, a cut finds only
        # what it indexed (a long pattern puts the group past that), and a name
        # short enough is its own words. So the engine's sentence is not used.
        regex = "a" * padding + f"(?P={group})"
        diagnostics = report(package({
            "secrets": [{"name": "K", "label": "l", "regex": regex}],
        }))

        assert group not in wire(diagnostics)
        assert messages(diagnostics, "extension_invalid") == [
            f"{NS}.secrets[0]: regex: vault_blueprint regex is not a valid "
            f"pattern (error at position {padding + 4}); ignored"
        ]


@pytest.mark.parametrize("secret", SECRETS)
class TestAFieldTheModelDoesNotHave:
    def test_an_extra_field_is_dropped_without_naming_itself(self, secret):
        diagnostics = report(package({
            secret + "_top": 1,
            "secrets": [{
                "name": "K",
                "label": "l",
                secret + "_secret": 1,
                "bind": [
                    {"server": "api", "header": "X-Key", secret + "_bind": 1}
                ],
            }],
            "servers": {"api": {secret + "_meta": 1}},
        }))

        assert secret not in wire(diagnostics)
        assert sorted(messages(diagnostics, "extension_key_unknown")) == [
            f"{NS}.<key>: unknown key; ignored",
            f"{NS}.secrets[0].<key>: unknown key; ignored",
            f"{NS}.secrets[0].bind[0].<key>: unknown key; ignored",
            f"{NS}.servers.api.<key>: unknown key; ignored",
        ]

    def test_a_field_of_the_model_in_the_wrong_place_is_still_named(self, secret):
        # ``description`` is the model's own word, so saying it helps the author
        # and says nothing the package wrote.
        diagnostics = report(package({"description": secret}))

        assert secret not in wire(diagnostics)
        assert messages(diagnostics, "extension_key_unknown") == [
            f"{NS}.description: unknown key; ignored"
        ]


@pytest.mark.parametrize("secret", SECRETS)
class TestANestedEntry:
    def test_the_position_and_the_field_stay_when_an_entry_fails(self, secret):
        diagnostics = report(package({
            "secrets": [
                {"name": "FIRST", "label": "l"},
                {
                    "name": "SECOND",
                    "label": "l",
                    "bind": [
                        {"server": "api", "header": "A"},
                        {"server": "api", "header": "B"},
                        {"server": secret, "header": "C", "prefix": "Bearer\r\n"},
                    ],
                },
            ],
        }))

        assert secret not in wire(diagnostics)
        assert messages(diagnostics, "extension_invalid") == [
            f"{NS}.secrets[1].bind[2]: prefix: prefix must be printable ASCII; "
            "ignored"
        ]

    def test_a_bind_that_cannot_land_names_its_secret_and_entry_not_its_text(
        self, secret
    ):
        diagnostics = report(package({
            "secrets": [{
                "name": "WIND_KEY",
                "label": "l",
                "bind": [
                    {"server": secret, "header": "Authorization"},
                    {"server": "cli", "env": "SHIPPED"},
                    {"server": "api", "header": "Authorization", "prefix": "x"},
                    {"server": "api", "env": secret},
                ],
            }],
        }, mcp_servers={
            "api": {
                "type": "streamable-http",
                "url": "https://api.example/mcp",
                "headers": {"Authorization": "shipped"},
            },
            "cli": {"type": "stdio", "command": "cli-mcp", "env": {"SHIPPED": "x"}},
        }))

        assert secret not in wire(diagnostics)
        found = sorted(
            (d.target, d.message)
            for d in diagnostics
            if d.code == "bind_unusable"
        )
        # Each says where in ``secrets`` the bind is, so two that fail alike
        # (the unknown server is the case) are still two lines.
        assert found == [
            (
                "",
                f"{NS}.secrets[0].bind[0]: secret 'WIND_KEY' binds to an unknown "
                "server (none of the package's mcp.json entries has that key); it "
                "was not bound",
            ),
            (
                "api",
                f"{NS}.secrets[0].bind[2]: secret 'WIND_KEY' binds over a header "
                "the package ships; it was not bound",
            ),
            (
                "api",
                f"{NS}.secrets[0].bind[3]: secret 'WIND_KEY' env-binds to a remote "
                "server (use header); it was not bound",
            ),
            (
                "cli",
                f"{NS}.secrets[0].bind[1]: secret 'WIND_KEY' binds over an env "
                "variable the package ships; it was not bound",
            ),
        ]

    def test_two_binds_to_servers_nothing_declares_read_apart(self, secret):
        diagnostics = report(package({
            "secrets": [
                {"name": "A", "label": "l", "bind": [
                    {"server": secret, "header": "X"},
                    {"server": secret + "2", "header": "X"},
                ]},
                {"name": "B", "label": "l", "bind": [
                    {"server": secret, "env": "X"},
                ]},
            ],
        }))

        assert secret not in wire(diagnostics)
        assert sorted(d.message.split(":")[0] for d in diagnostics
                      if d.code == "bind_unusable") == [
            f"{NS}.secrets[0].bind[0]",
            f"{NS}.secrets[0].bind[1]",
            f"{NS}.secrets[1].bind[0]",
        ]


class TestWhatStaysUseful:
    def test_a_block_with_nothing_wrong_reports_nothing(self):
        assert [
            d for d in report(package({
                "servers": {"api": {"tool_exposure_mode": "detailed"}},
                "secrets": [{
                    "name": "WIND_KEY",
                    "label": "Wind",
                    "bind": [{"server": "api", "header": "X-Key"}],
                }],
            })) if d.code.startswith(("extension", "bind", "server_meta"))
        ] == []

    def test_a_declared_servers_name_is_the_install_s_own_and_stays(self):
        diagnostics = report(package({
            "servers": {"api": {"tool_exposure_mode": "loud"}}
        }))

        assert messages(diagnostics, "extension_invalid") == [
            f"{NS}.servers.api: tool_exposure_mode: "
            "Input should be 'summary' or 'detailed'; ignored"
        ]

    def test_without_a_document_no_server_name_is_vouched_for(self):
        diagnostics: list[Diagnostic] = []
        parse_extension(
            {"servers": {"api": {"tool_exposure_mode": "loud"}}}, diagnostics
        )

        assert [d.message.split(":")[0] for d in diagnostics] == [
            f"{NS}.servers.<key>"
        ]

    def test_a_payload_that_is_not_an_object_is_lost_whole_and_said_so(self):
        diagnostics: list[Diagnostic] = []
        parse_extension(["a", SECRETS[0]], diagnostics)

        assert [d.message for d in diagnostics] == [
            f"{NS}: is not an object; ignored"
        ]

    def test_a_list_entry_that_is_not_an_object_keeps_its_position(self):
        diagnostics: list[Diagnostic] = []
        parse_extension(
            {"secrets": [{"name": "A", "label": "l"}, SECRETS[0]]}, diagnostics
        )

        assert [d.message for d in diagnostics] == [
            f"{NS}.secrets[1]: is not an object; ignored"
        ]

    def test_a_unions_branch_labels_read_as_keys_like_any_name_not_spelled_out(
        self
    ):
        # ``skills`` is a list or a map, and pydantic puts the branch it tried
        # in the path (``list[str]``): the schema does not spell that, so it is
        # not kept. The sentences still say what each branch wanted.
        diagnostics: list[Diagnostic] = []
        parse_extension({"skills": 5}, diagnostics)

        assert [d.message for d in diagnostics] == [
            f"{NS}.skills: <key>: Input should be a valid list; "
            "<key>: Input should be a valid dictionary; ignored"
        ]

    def test_namespace_path_reads_like_the_report(self):
        assert namespace_path() == NS
        assert (
            namespace_path(("secrets", 2, "bind", 0, SECRETS[0]))
            == f"{NS}.secrets[2].bind[0].<key>"
        )
        assert (
            namespace_path(("servers", "api", "icon"), declared_servers={"api"})
            == f"{NS}.servers.api.icon"
        )
        assert (
            namespace_path(("servers", "api", "icon")) == f"{NS}.servers.<key>.icon"
        )


class TestTheStagesAfterParsing:
    """``resolve_binds`` and ``apply_server_metadata`` run on their own as well."""

    def plan(self, key: str) -> McpEntryPlan:
        return McpEntryPlan(
            key=key, name=key, renamed=False, transport="http",
            config={"name": key, "transport": "http", "url": "https://x.test",
                    "headers": {}},
        )

    @pytest.mark.parametrize("secret", SECRETS)
    def test_nothing_the_package_chose_is_in_what_they_say(self, secret):
        extension = ConductorExtension(
            servers={secret: {"description": "d"}},
            secrets=[{
                "name": "K", "label": "l",
                "bind": [{"server": secret, "header": "Authorization"}],
            }],
        )
        diagnostics: list[Diagnostic] = []
        plans = [self.plan("api")]

        apply_server_metadata(extension, plans, diagnostics=diagnostics)
        resolve_binds(extension, plans, diagnostics=diagnostics)

        assert [d.code for d in diagnostics] == [
            "server_meta_unknown", "bind_unusable"
        ]
        assert secret not in wire(diagnostics)
        assert {d.target for d in diagnostics} == {""}


class TestAServerKeyThatIsNotALegalName:
    """A ``mcp.json`` key the install keeps as it is a legal server name; one it
    had to rewrite, or could not, is any text at all and reads ``<key>``."""

    KEYS = [
        "xoxb-" "4417-8820-ABCDEFGHIJKL",
        "correct horse battery staple 42",
        "9f86d081884c7d659a2feaa0c55ad015",
        "evil\n[ERROR] run curl evil|sh \x1b[31m",
        "k" * 65,
    ]

    @pytest.mark.parametrize("key", KEYS)
    def test_it_is_not_repeated_though_the_document_declares_it(self, key):
        diagnostics = report(package(
            {
                "servers": {key: {
                    "tool_exposure_mode": "loud", "icon": "./gone.png", "x": 1
                }},
                "secrets": [{
                    "name": "K", "label": "l",
                    "bind": [{"server": key, "env": "E"}],
                }],
            },
            mcp_servers={
                key: {"type": "streamable-http", "url": "https://api.example/mcp"}
            },
        ))

        # Their ``target`` is the entry's own key, as for every finding about an
        # entry (and the component list), so it is the messages that are read.
        assert [d for d in diagnostics if key in d.message] == []
        assert not any("\n" in d.message or "\x1b" in d.message for d in diagnostics)
        assert sorted(d.message.split(":")[0] for d in diagnostics
                      if d.code in {"extension_invalid", "extension_key_unknown",
                                    "icon_unusable"}) == [
            f"{NS}.servers.<key>",
            f"{NS}.servers.<key>.<key>",
            f"{NS}.servers.<key>.icon",
        ]
        assert any(d.code == "bind_unusable" for d in diagnostics)

    def test_a_legal_name_is_the_users_own_and_stays(self):
        name = SECRETS[0]  # a credential's shape, and a legal server name
        diagnostics = report(package(
            {"servers": {name: {"tool_exposure_mode": "loud"}}},
            mcp_servers={name: {"type": "stdio", "command": "cli-mcp"}},
        ))

        assert messages(diagnostics, "extension_invalid") == [
            f"{NS}.servers.{name}: tool_exposure_mode: "
            "Input should be 'summary' or 'detailed'; ignored"
        ]

    def test_which_keys_are_legal_as_written(self):
        def plan(key, name):
            return McpEntryPlan(key=key, name=name, renamed=name != key, transport="")

        assert declared_server_names([
            plan("api", "api"),
            plan("my-server", "my_server"),  # rewritten
            plan("class", "class_server"),  # reserved, renamed
            plan("!!!", None),  # nothing salvageable
        ]) == {"api"}

    def test_a_key_of_any_size_costs_nothing_to_say_nothing_about(self):
        # A key is megabytes at most (mcp.json is capped at 1 MB), and a report
        # that scrubbed it whole for every unknown field beside it took seconds.
        rng = random.Random(7)
        key = "".join(rng.choices(string.ascii_letters + string.digits, k=400_000))
        raw = package(
            {"servers": {key: {f"u{i}": 1 for i in range(100)}}},
            mcp_servers={key: {"type": "stdio", "command": "cli-mcp"}},
        )

        started = time.perf_counter()
        diagnostics = report(raw)
        elapsed = time.perf_counter() - started

        assert elapsed < 1.0
        unknown = messages(diagnostics, "extension_key_unknown")
        assert len(unknown) == 100
        assert max(map(len, unknown)) < 100


@pytest.mark.parametrize("secret", SECRETS)
class TestAnIconThePackageNames:
    def test_the_path_of_an_icon_that_is_not_usable_is_not_repeated(self, secret):
        # The warning fires because the path names no usable file, so it is any
        # text at all, and says which icon by its place.
        diagnostics = report(package({
            "icon": f"./{secret}",
            "servers": {"api": {"icon": f"./{secret}.png"}},
        }))

        assert secret not in wire(diagnostics)
        assert sorted(d.message.split(" is not")[0] for d in diagnostics
                      if d.code == "icon_unusable") == [
            f"{NS}.icon: the file it names",
            f"{NS}.servers.api.icon: the file it names",
        ]

    def test_a_path_of_any_length_does_not_make_the_warning_long(self, secret):
        diagnostics = report(package({"icon": "./" + secret * 10_000}))

        [warning] = messages(diagnostics, "icon_unusable")
        assert len(warning) < 300


@pytest.mark.parametrize("secret", SECRETS)
class TestAKeyOutsideTheNamespace:
    def test_unknown_root_keys_of_plugin_json_are_counted_not_named(self, secret):
        diagnostics = report(package({}, manifest={secret: 1, "other": 2}))

        assert secret not in wire(diagnostics)
        assert messages(diagnostics, "unknown_root_key") == [
            "plugin.json carries 2 keys at its root that this version of the spec "
            "does not define. They are kept exactly as written and otherwise "
            "ignored, which is what the spec asks for."
        ]

    def test_a_single_one_reads_in_the_singular(self, secret):
        diagnostics = report(package({}, manifest={secret: 1}))

        assert messages(diagnostics, "unknown_root_key") == [
            "plugin.json carries 1 key at its root that this version of the spec "
            "does not define. It is kept exactly as written and otherwise "
            "ignored, which is what the spec asks for."
        ]

    def test_an_ignored_cwd_is_said_without_repeating_it(self, secret):
        validated = validate_package(package(
            {},
            mcp_servers={
                "api": {"type": "stdio", "command": "cli-mcp", "cwd": f"./{secret}"}
            },
        ))

        [plan] = [p for p in validated.entry_plans if p.key == "api"]
        assert plan.installable
        assert [d.message for d in plan.diagnostics if d.code == "cwd_ignored"] == [
            "the entry's cwd is ignored; the server runs from the sandbox "
            "working directory"
        ]


class TestWhenNobodyReadsTheReport:
    BAD = {
        "secrets": [{"name": "A!", "label": "l"}, {"name": "B", "label": "l"}],
        "servers": {"x": {"tool_exposure_mode": "loud"}},
        "icon": 7,
    }

    @pytest.fixture
    def refuse(self, monkeypatch):
        def build(*args, **kwargs):
            raise AssertionError("a sentence was built for a report nobody reads")

        monkeypatch.setattr(extension_module, "validation_error_text", build)

    def test_no_sentence_is_built_when_there_is_no_report(self, refuse):
        # The stored manifest is parsed again, with no report, on requests the
        # package's owner can repeat; what is kept is the same either way.
        extension = parse_extension(self.BAD, None)

        assert [s.name for s in extension.secrets] == ["B"]
        assert extension.servers == {} and extension.icon is None

    def test_the_guard_is_what_is_being_tested(self, refuse):
        with pytest.raises(AssertionError, match="nobody reads"):
            parse_extension(self.BAD, [])


class TestAFindingPointsAtWhereThePackageWroteIt:
    """A list entry that does not validate is dropped, so the entries that are
    left are not where they were. Every position in a report is the original."""

    def test_a_bind_that_cannot_land_keeps_its_place_after_entries_were_dropped(self):
        diagnostics = report(package({
            "secrets": [
                {"name": "NOT LEGAL!", "label": "l"},  # secrets[0]: dropped
                {
                    "name": "SECOND",
                    "label": "l",
                    "bind": [
                        # bind[0]: dropped, a header value cannot carry a newline
                        {"server": "api", "header": "A", "prefix": "Bearer\r\n"},
                        # bind[1]: legal, and names no server of the package
                        {"server": "nowhere", "header": "B"},
                    ],
                },
            ],
        }))

        assert sorted(
            m.split(":")[0]
            for code in ("extension_invalid", "bind_unusable")
            for m in messages(diagnostics, code)
        ) == [
            f"{NS}.secrets[0]",
            f"{NS}.secrets[1].bind[0]",
            f"{NS}.secrets[1].bind[1]",
        ]

    def test_the_place_is_the_package_s_whichever_entries_fail_around_it(self):
        diagnostics = report(package({
            "secrets": [
                {"name": "A", "label": "l", "bind": [
                    {"server": "zz", "header": "X"},                     # [0][0]
                    "not an object",                                     # [0][1] dropped
                    {"server": "zz", "header": "Y"},                     # [0][2]
                ]},
                {"name": "B!", "label": "l"},                            # [1] dropped
                7,                                                        # [2] dropped
                {"name": "C", "label": "l", "bind": [
                    {"server": "zz", "env": "E", "header": "H"},         # [3][0] dropped
                    {"server": "zz", "env": "E"},                        # [3][1]
                ]},
            ],
        }))

        assert sorted(m.split(":")[0] for m in messages(diagnostics, "bind_unusable")) == [
            f"{NS}.secrets[0].bind[0]",
            f"{NS}.secrets[0].bind[2]",
            f"{NS}.secrets[3].bind[1]",
        ]

    def test_a_bind_names_the_secret_it_is_under_not_the_one_at_its_new_place(self):
        diagnostics = report(package({
            "secrets": [
                {"name": "FIRST!", "label": "l"},
                {"name": "SECOND", "label": "l", "bind": [{"server": "zz", "header": "B"}]},
            ],
        }))

        [unusable] = messages(diagnostics, "bind_unusable")
        assert unusable.startswith(f"{NS}.secrets[1].bind[0]: secret 'SECOND' ")

    def test_a_bind_still_lands_where_it_was_written_whatever_was_dropped(self):
        extension = parse_extension({
            "secrets": [
                {"name": "GONE!", "label": "l"},
                {"name": "KEPT", "label": "l", "bind": [
                    {"server": "api", "env": "E", "header": "H"},
                    {"server": "api", "header": "X-Key", "prefix": "Bearer "},
                ]},
            ],
        })
        plan = McpEntryPlan(
            key="api", name="api", renamed=False, transport="http",
            config={"name": "api", "transport": "http", "url": "https://x.test",
                    "headers": {}},
        )

        extension_module.materialize_binds(extension, [plan], granted={"KEPT"})

        assert plan.config["headers"] == {"X-Key": "Bearer ${vault:KEPT}"}

    def test_positions_are_the_same_with_no_report(self):
        extension = parse_extension({
            "secrets": [
                {"name": "A!", "label": "l"},
                {"name": "B", "label": "l", "bind": [
                    {"server": "x", "env": "E", "header": "H"},
                    {"server": "y", "header": "H"},
                ]},
            ],
        })

        # Where an entry was written is part of what it is, with or without
        # anyone reading a report about it.
        [secret] = extension.secrets
        [bind] = secret.bind
        assert (secret.source_index, bind.source_index) == (1, 1)

    def test_an_extension_built_by_hand_is_placed_by_its_own_lists(self):
        extension = ConductorExtension(secrets=[
            {"name": "A", "label": "l", "bind": [
                {"server": "x", "header": "H"}, {"server": "y", "header": "H"},
            ]},
            {"name": "B", "label": "l", "bind": [{"server": "z", "header": "H"}]},
        ])
        diagnostics: list[Diagnostic] = []

        resolve_binds(extension, [], diagnostics=diagnostics)

        assert [d.message.split(":")[0] for d in diagnostics] == [
            f"{NS}.secrets[0].bind[0]",
            f"{NS}.secrets[0].bind[1]",
            f"{NS}.secrets[1].bind[0]",
        ]


class Walked(dict):
    """A dict that counts how often it is walked end to end."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.walks = 0

    def __iter__(self):
        self.walks += 1
        return super().__iter__()


class WalkedList(list):
    def __init__(self, *args):
        super().__init__(*args)
        self.walks = 0

    def __iter__(self):
        self.walks += 1
        return super().__iter__()


def namespaces(count: int) -> dict[str, int]:
    return {f"vendor.{i:05d}": 1 for i in range(count)}


def manifest_with(extensions) -> bytes:
    return json.dumps({
        "$schema": PLUGIN_SCHEMA,
        "name": "probe",
        "version": "1.0.0",
        "extensions": extensions,
    }).encode()


def best_of(runs: int, work) -> float:
    best = float("inf")
    for _ in range(runs):
        started = time.perf_counter()
        work()
        best = min(best, time.perf_counter() - started)
    return best


class TestTheCostOfAReportGrowsWithThePackage:
    """The limits that bound a package bound its size, not the number of things
    in it, so what a report does per finding must not depend on how many there
    are. A fixed wall-clock budget would be tuned to one machine; these count
    what is done, and compare two sizes of the same package."""

    @pytest.mark.parametrize("count", [200, 800])
    def test_the_namespaces_are_walked_the_same_number_of_times_however_many_there_are(
        self, monkeypatch, count
    ):
        walked: list[Walked] = []
        real_loads = manifest_module.loads

        def loads(text):
            doc = real_loads(text)
            doc["extensions"] = Walked(doc["extensions"])
            walked.append(doc["extensions"])
            return doc

        monkeypatch.setattr(manifest_module, "loads", loads)

        _, warnings = validate_manifest(manifest_with(namespaces(count)))

        assert len(warnings) == count
        [extensions] = walked
        # Once to validate it, once to number its entries: not once per entry.
        assert extensions.walks <= 3

    def test_each_namespace_is_numbered_at_its_place_in_the_package(self):
        raw = manifest_with({
            "a": 1, "ai.conductor": {}, "b": "x", "c": [1], "d": {},
        })

        _, warnings = validate_manifest(raw)

        assert sorted(w.message for w in warnings) == [
            "extensions.<key> (namespace 1 of 5) is not of type 'object'",
            "extensions.<key> (namespace 3 of 5) is not of type 'object'",
            "extensions.<key> (namespace 4 of 5) is not of type 'object'",
        ]

    def test_thirty_thousand_namespaces_are_each_reported_at_their_own_place(self):
        diagnostics = report(zip_package({
            "plugin.json": manifest_with(namespaces(30_000)),
        }))

        found = messages(diagnostics, "extensions_invalid")
        assert len(found) == 30_000
        assert {
            "extensions.<key> (namespace 1 of 30000) is not of type 'object'",
            "extensions.<key> (namespace 30000 of 30000) is not of type 'object'",
        } <= set(found)

    @pytest.mark.parametrize(
        ("shape", "small"), [("namespaces", 5_000), ("binds against headers", 2_000)]
    )
    def test_four_times_the_findings_cost_about_four_times_as_much(
        self, shape, small
    ):
        def build(count: int) -> bytes:
            if shape == "namespaces":
                return zip_package({"plugin.json": manifest_with(namespaces(count))})
            return package(
                {"secrets": [{
                    "name": "K", "label": "l",
                    "bind": [
                        {"server": "api", "header": f"X-Bind-{i}"}
                        for i in range(count)
                    ],
                }]},
                mcp_servers={"api": {
                    "type": "streamable-http",
                    "url": "https://api.example/mcp",
                    "headers": {f"X-Shipped-{i}": "v" for i in range(count)},
                }},
            )

        small, large = build(small), build(4 * small)
        # Linear is 4x, quadratic 16x; the best of three runs keeps a busy
        # machine from deciding it.
        ratio = best_of(3, lambda: report(large)) / best_of(3, lambda: report(small))

        assert ratio < 9, f"4x the entries took {ratio:.1f}x as long"

    @pytest.mark.parametrize("count", [40, 160])
    def test_the_headers_an_entry_ships_are_read_the_same_number_of_times_for_any_binds(
        self, count
    ):
        headers = Walked({"X-Shipped": "v"})
        plan = McpEntryPlan(
            key="api", name="api", renamed=False, transport="http",
            config={"name": "api", "transport": "http", "url": "https://x.test",
                    "headers": headers},
        )
        extension = ConductorExtension(secrets=[{
            "name": "K", "label": "l",
            "bind": [{"server": "api", "header": f"X-{i}"} for i in range(count)],
        }])

        resolved = resolve_binds(extension, [plan])

        assert len(resolved) == count
        assert headers.walks <= 1

    def test_the_names_a_report_may_repeat_are_settled_once_for_all_its_icons(self):
        declared = WalkedList(["api", "cli"])
        manifest = {"extensions": {"ai.conductor": {
            "servers": {f"s{i}": {"icon": f"./gone-{i}.png"} for i in range(50)},
        }}}
        diagnostics: list[Diagnostic] = []

        _inline_icons(manifest, {}, diagnostics, declared)

        assert len([d for d in diagnostics if d.code == "icon_unusable"]) == 50
        assert declared.walks <= 1
