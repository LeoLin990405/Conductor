"""Extract symbols, calls, and imports with tree-sitter.

Only the MIT tree-sitter runtime and the official Python and TypeScript
grammars are used. Names that are computed at runtime (getattr, subscript
calls, string keys) are left unresolved on purpose.
"""

from __future__ import annotations

from tree_sitter import Language, Node, Parser

from .model import (
    Call,
    FileFacts,
    ImpactError,
    Import,
    Symbol,
    language_for_path,
    parent_qual,
    qualified,
)

_PARSERS: dict[str, Parser] = {}
_DEF_TYPES = frozenset(
    {
        "function_definition",
        "class_definition",
        "function_declaration",
        "class_declaration",
        "method_definition",
        "async_function_definition",
    }
)
_FUNC_TYPES = frozenset(
    {
        "function_definition",
        "function_declaration",
        "method_definition",
        "async_function_definition",
        # Generator declarations are functions too; the anonymous default
        # form (`export default function* () {}`) goes through the no-name path.
        "generator_function",
        "generator_function_declaration",
    }
)
_FUNC_VALUES = frozenset({"arrow_function", "function_expression", "function"})
_NAME_TYPES = frozenset(
    {
        "identifier",
        "property_identifier",
        "type_identifier",
        "private_property_identifier",
    }
)
_CALL_TYPES = frozenset({"call", "call_expression"})
_MEMBER_TYPES = frozenset({"attribute", "member_expression"})
# Python 3 runs these as nested functions. They do not see the class body.
_COMPREHENSIONS = frozenset(
    {
        "list_comprehension",
        "set_comprehension",
        "dictionary_comprehension",
        "generator_expression",
    }
)


def extract_path(rel: str, source: bytes) -> FileFacts:
    language = language_for_path(rel)
    if language is None:
        raise ImpactError(f"unsupported source file: {rel}")
    return extract_source(rel, source, language)


def extract_source(rel: str, source: bytes, language: str) -> FileFacts:
    parser = _parser(language)
    tree = parser.parse(_utf8(source))
    symbols: list[Symbol] = []
    calls: list[Call] = []
    imports: list[Import] = []
    _walk(
        tree.root_node,
        rel=rel,
        scope=[],
        caller="",
        enclosing_class="",
        in_class_body=False,
        export_kind="",
        range_start=None,
        symbols=symbols,
        calls=calls,
        imports=imports,
    )
    return FileFacts(language, tuple(symbols), tuple(calls), tuple(imports))


def _parser(language: str) -> Parser:
    cached = _PARSERS.get(language)
    if cached is not None:
        return cached
    try:
        import tree_sitter_python as python_grammar
        import tree_sitter_typescript as typescript_grammar
    except ImportError as exc:
        raise ImpactError(
            "tree-sitter grammars are not installed "
            "(need tree-sitter, tree-sitter-python, and tree-sitter-typescript)"
        ) from exc
    if language == "python":
        lang = Language(python_grammar.language())
    elif language == "typescript":
        lang = Language(typescript_grammar.language_typescript())
    elif language == "tsx":
        lang = Language(typescript_grammar.language_tsx())
    else:
        raise ImpactError(f"unsupported language: {language}")
    parser = Parser(lang)
    _PARSERS[language] = parser
    return parser


def _utf8(source: bytes) -> bytes:
    try:
        source.decode("utf-8")
    except UnicodeDecodeError:
        return source.decode("utf-8", "replace").encode("utf-8")
    return source


def _text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", "replace")


def _line(node: Node) -> int:
    return node.start_point[0] + 1


def _column(node: Node) -> int:
    return node.start_point[1]


def _end_line(node: Node) -> int:
    row, col = node.end_point
    if col == 0:
        return max(node.start_point[0] + 1, row)
    return row + 1


def _node_name(node: Node) -> str:
    named = node.child_by_field_name("name")
    if named is not None:
        return _text(named)
    for child in node.named_children:
        if child.type in _NAME_TYPES:
            return _text(child)
    return ""


def _unquote(node: Node | None) -> str:
    raw = _text(node).strip()
    if len(raw) >= 2 and raw[0] in "\"'`" and raw[-1] == raw[0]:
        return raw[1:-1]
    return raw


def _is_keyword(node: Node, word: str) -> bool:
    return (not node.is_named or node.type == word) and _text(node) == word


def _lexical_caller(caller: str) -> str:
    """Caller a nested function sees. Class-body execution is not a scope."""
    if not caller.endswith("#body"):
        return caller
    base = caller[: -len("#body")]
    qual = base.split("::", 1)[-1]
    if "." not in qual:
        return ""
    return base.rpartition(".")[0]


def _import_scope(caller: str, enclosing_class: str, in_class_body: bool) -> str:
    """Scope an import binds in. A class body is not a scope methods look through.

    Its own marker is in no function's scope chain, so a method keeps seeing
    the module binding, as Python resolves it.
    """
    if in_class_body and enclosing_class:
        return f"{enclosing_class}#body"
    return caller


def _walk(
    node: Node,
    *,
    rel: str,
    scope: list[str],
    caller: str,
    enclosing_class: str,
    in_class_body: bool,
    export_kind: str,
    range_start: int | None,
    symbols: list[Symbol],
    calls: list[Call],
    imports: list[Import],
) -> None:
    ntype = node.type
    if ntype == "import_statement":
        owner = _import_scope(caller, enclosing_class, in_class_body)
        if _looks_like_python_import(node):
            imports.extend(_python_import(node, rel, owner))
        else:
            imports.extend(_ts_import(node, rel, owner))
        return
    if ntype == "import_from_statement":
        imports.extend(_python_from(node, rel, _import_scope(caller, enclosing_class, in_class_body)))
        return
    if ntype == "decorated_definition":
        _walk_decorated(
            node,
            rel=rel,
            scope=scope,
            caller=caller,
            enclosing_class=enclosing_class,
            in_class_body=in_class_body,
            export_kind=export_kind,
            symbols=symbols,
            calls=calls,
            imports=imports,
        )
        return
    if ntype == "export_statement":
        _walk_export(
            node,
            rel=rel,
            scope=scope,
            caller=caller,
            enclosing_class=enclosing_class,
            in_class_body=in_class_body,
            symbols=symbols,
            calls=calls,
            imports=imports,
        )
        return
    if ntype in {"class_definition", "class_declaration"}:
        _define_class(
            node,
            rel=rel,
            scope=scope,
            caller=caller,
            enclosing_class=enclosing_class,
            export_kind=export_kind,
            range_start=range_start,
            symbols=symbols,
            calls=calls,
            imports=imports,
        )
        return
    if ntype in _FUNC_TYPES:
        _define_function(
            node,
            rel=rel,
            scope=scope,
            caller=caller,
            enclosing_class=enclosing_class,
            in_class_body=in_class_body,
            export_kind=export_kind,
            range_start=range_start,
            symbols=symbols,
            calls=calls,
            imports=imports,
        )
        return
    if ntype in {"variable_declarator", "public_field_definition"}:
        if _define_function_value(
            node,
            rel=rel,
            scope=scope,
            caller=caller,
            enclosing_class=enclosing_class,
            in_class_body=in_class_body,
            export_kind=export_kind,
            symbols=symbols,
            calls=calls,
            imports=imports,
        ):
            return
    if ntype in {"arrow_function", "function_expression"}:
        # Anonymous functions have no name of their own. A default export is
        # still a bindable symbol; other anonymous functions are not.
        body_caller = caller
        body_scope = scope
        if export_kind == "default":
            synthetic = qualified(rel, scope, "default")
            symbols.append(
                Symbol(
                    rel,
                    "default",
                    synthetic,
                    "function",
                    range_start or _line(node),
                    _end_line(node),
                    parent_qual(rel, scope),
                    "default",
                    _column(node),
                )
            )
            body_caller = synthetic
            body_scope = [*scope, "default"]
            _record_parameters(node, rel, synthetic, symbols)
            scope = [*scope, "default"]
            caller = synthetic
        # Defaults and the body both belong to the enclosing caller; defaults
        # must still be walked.
        _walk_signature_and_body(
            node,
            rel=rel,
            scope=scope,
            caller=caller,
            enclosing_class=enclosing_class,
            body_scope=body_scope,
            body_caller=body_caller,
            symbols=symbols,
            calls=calls,
            imports=imports,
        )
        return
    if ntype in _CALL_TYPES:
        _record_call(node, rel, caller, enclosing_class, calls)
        _walk_children(
            node,
            rel=rel,
            scope=scope,
            caller=caller,
            enclosing_class=enclosing_class,
            in_class_body=in_class_body,
            export_kind="",
            symbols=symbols,
            calls=calls,
            imports=imports,
        )
        return
    if ntype == "new_expression":
        ctor = node.child_by_field_name("constructor")
        if ctor is not None:
            _record_callee_node(ctor, rel, caller, enclosing_class, calls, _line(node))
        _walk_children(
            node,
            rel=rel,
            scope=scope,
            caller=caller,
            enclosing_class=enclosing_class,
            in_class_body=False,
            export_kind="",
            symbols=symbols,
            calls=calls,
            imports=imports,
        )
        return
    if ntype in {"jsx_opening_element", "jsx_self_closing_element"}:
        _record_jsx(node, rel, caller, enclosing_class, calls)
        _walk_children(
            node,
            rel=rel,
            scope=scope,
            caller=caller,
            enclosing_class=enclosing_class,
            in_class_body=False,
            export_kind="",
            symbols=symbols,
            calls=calls,
            imports=imports,
        )
        return
    # A class body executes where it is written. A lambda or comprehension
    # defined there does not: its calls keep the enclosing function's names.
    if caller.endswith("#body") and ntype in _COMPREHENSIONS:
        _walk_children(
            node,
            rel=rel,
            scope=scope,
            caller=_lexical_caller(caller),
            enclosing_class=enclosing_class,
            in_class_body=False,
            export_kind="",
            symbols=symbols,
            calls=calls,
            imports=imports,
        )
        return
    if caller.endswith("#body") and ntype == "lambda":
        body = node.child_by_field_name("body")
        lexical = _lexical_caller(caller)
        for child in node.children:
            if body is not None and child == body:
                continue
            _walk(
                child,
                rel=rel,
                scope=scope,
                caller=caller,
                enclosing_class=enclosing_class,
                in_class_body=in_class_body,
                export_kind="",
                range_start=None,
                symbols=symbols,
                calls=calls,
                imports=imports,
            )
        if body is not None:
            _walk(
                body,
                rel=rel,
                scope=scope,
                caller=lexical,
                enclosing_class=enclosing_class,
                in_class_body=False,
                export_kind="",
                range_start=None,
                symbols=symbols,
                calls=calls,
                imports=imports,
            )
        return
    _walk_children(
        node,
        rel=rel,
        scope=scope,
        caller=caller,
        enclosing_class=enclosing_class,
        in_class_body=in_class_body,
        export_kind=export_kind,
        symbols=symbols,
        calls=calls,
        imports=imports,
    )


def _walk_children(
    node: Node,
    *,
    rel: str,
    scope: list[str],
    caller: str,
    enclosing_class: str,
    in_class_body: bool,
    export_kind: str,
    symbols: list[Symbol],
    calls: list[Call],
    imports: list[Import],
    skip: set[int] | None = None,
) -> None:
    for child in node.children:
        if skip and id(child) in skip:
            continue
        _walk(
            child,
            rel=rel,
            scope=scope,
            caller=caller,
            enclosing_class=enclosing_class,
            in_class_body=in_class_body,
            export_kind=export_kind,
            range_start=None,
            symbols=symbols,
            calls=calls,
            imports=imports,
        )


def _walk_decorated(
    node: Node,
    *,
    rel: str,
    scope: list[str],
    caller: str,
    enclosing_class: str,
    in_class_body: bool,
    export_kind: str,
    symbols: list[Symbol],
    calls: list[Call],
    imports: list[Import],
) -> None:
    definition = next((child for child in node.named_children if child.type in _DEF_TYPES), None)
    if definition is None:
        _walk_children(
            node,
            rel=rel,
            scope=scope,
            caller=caller,
            enclosing_class=enclosing_class,
            in_class_body=in_class_body,
            export_kind=export_kind,
            symbols=symbols,
            calls=calls,
            imports=imports,
        )
        return
    defined = _peek_qual(definition, rel, scope, in_class_body)
    for child in node.named_children:
        if child.type == "decorator":
            _walk_decorator(child, rel, defined, enclosing_class, scope, calls, symbols, imports)
        else:
            _walk(
                child,
                rel=rel,
                scope=scope,
                caller=caller,
                enclosing_class=enclosing_class,
                in_class_body=in_class_body,
                export_kind=export_kind,
                range_start=_line(node),
                symbols=symbols,
                calls=calls,
                imports=imports,
            )


def _walk_decorator(
    node: Node,
    rel: str,
    caller: str,
    enclosing_class: str,
    scope: list[str],
    calls: list[Call],
    symbols: list[Symbol],
    imports: list[Import],
) -> None:
    expr = next(iter(node.named_children), None)
    if expr is None:
        return
    if expr.type in _CALL_TYPES:
        _record_call(expr, rel, caller, enclosing_class, calls)
        arguments = expr.child_by_field_name("arguments")
        if arguments is not None:
            _walk(
                arguments,
                rel=rel,
                scope=scope,
                caller=caller,
                enclosing_class=enclosing_class,
                in_class_body=False,
                export_kind="",
                range_start=None,
                symbols=symbols,
                calls=calls,
                imports=imports,
            )
        return
    _record_callee_node(expr, rel, caller, enclosing_class, calls, _line(expr))


def _walk_export(
    node: Node,
    *,
    rel: str,
    scope: list[str],
    caller: str,
    enclosing_class: str,
    in_class_body: bool,
    symbols: list[Symbol],
    calls: list[Call],
    imports: list[Import],
) -> None:
    kind = "default" if any(_is_keyword(child, "default") for child in node.children) else "named"
    for child in node.named_children:
        if child.type == "identifier" and kind == "default":
            _mark_exported(symbols, rel, scope, _text(child), "default")
            continue
        if child.type in {"export_clause", "export_specifier"}:
            _mark_export_clause(child, symbols, rel, scope)
            continue
        _walk(
            child,
            rel=rel,
            scope=scope,
            caller=caller,
            enclosing_class=enclosing_class,
            in_class_body=in_class_body,
            export_kind=kind,
            range_start=None,
            symbols=symbols,
            calls=calls,
            imports=imports,
        )


def _mark_export_clause(node: Node, symbols: list[Symbol], rel: str, scope: list[str]) -> None:
    if node.type == "export_specifier":
        name = node.child_by_field_name("name")
        if name is not None:
            _mark_exported(symbols, rel, scope, _text(name), "named")
        return
    for child in node.named_children:
        _mark_export_clause(child, symbols, rel, scope)


def _mark_exported(
    symbols: list[Symbol], rel: str, scope: list[str], name: str, exported: str
) -> None:
    parent = parent_qual(rel, scope)
    for index, symbol in enumerate(symbols):
        if symbol.path == rel and symbol.name == name and symbol.parent == parent:
            symbols[index] = Symbol(
                symbol.path,
                symbol.name,
                symbol.qualified_name,
                symbol.kind,
                symbol.start_line,
                symbol.end_line,
                symbol.parent,
                exported,
                symbol.column,
            )


def _define_class(
    node: Node,
    *,
    rel: str,
    scope: list[str],
    caller: str,
    enclosing_class: str,
    export_kind: str,
    range_start: int | None,
    symbols: list[Symbol],
    calls: list[Call],
    imports: list[Import],
) -> None:
    del enclosing_class
    name = _node_name(node)
    if not name:
        return
    qual = qualified(rel, scope, name)
    symbols.append(
        Symbol(
            rel,
            name,
            qual,
            "class",
            range_start or _line(node),
            _end_line(node),
            parent_qual(rel, scope),
            export_kind,
            _column(node),
        )
    )
    body = node.child_by_field_name("body")
    for child in node.children:
        if body is not None and child == body:
            continue
        _walk(
            child,
            rel=rel,
            scope=scope,
            caller=caller,
            enclosing_class="",
            in_class_body=False,
            export_kind="",
            range_start=None,
            symbols=symbols,
            calls=calls,
            imports=imports,
        )
    if body is not None:
        # ``Qual#body`` is on no method's scope chain. Calls written in the
        # class body still use it, so ``from alt import helper`` then
        # ``value = helper()`` binds ``alt.helper``.
        _walk(
            body,
            rel=rel,
            scope=[*scope, name],
            caller=f"{qual}#body",
            enclosing_class=qual,
            in_class_body=True,
            export_kind="",
            range_start=None,
            symbols=symbols,
            calls=calls,
            imports=imports,
        )


def _define_function(
    node: Node,
    *,
    rel: str,
    scope: list[str],
    caller: str,
    enclosing_class: str,
    in_class_body: bool,
    export_kind: str,
    range_start: int | None,
    symbols: list[Symbol],
    calls: list[Call],
    imports: list[Import],
) -> None:
    name = _node_name(node)
    if not name:
        if export_kind == "default":
            # `export default function () {}` has no name; the default binding
            # is the only handle callers have, so record it as one symbol and
            # let its body calls belong to it.
            start = range_start or _line(node)
            synthetic = qualified(rel, scope, "default")
            symbols.append(
                Symbol(
                    rel,
                    "default",
                    synthetic,
                    "function",
                    start,
                    _end_line(node),
                    parent_qual(rel, scope),
                    "default",
                )
            )
            _record_parameters(node, rel, synthetic, symbols)
            _walk_signature_and_body(
                node,
                rel=rel,
                scope=[*scope, "default"],
                caller=synthetic,
                enclosing_class=enclosing_class,
                body_scope=[*scope, "default"],
                body_caller=synthetic,
                symbols=symbols,
                calls=calls,
                imports=imports,
            )
            return
        body = node.child_by_field_name("body")
        if body is not None:
            _walk(
                body,
                rel=rel,
                scope=scope,
                caller=caller,
                enclosing_class=enclosing_class,
                in_class_body=False,
                export_kind="",
                range_start=None,
                symbols=symbols,
                calls=calls,
                imports=imports,
            )
        return
    kind = "method" if in_class_body else "function"
    qual = qualified(rel, scope, name)
    # Python defaults run when the def statement executes, in the enclosing
    # scope. TypeScript defaults run when the function is called.
    symbols.append(
        Symbol(
            rel,
            name,
            qual,
            kind,
            range_start or _line(node),
            _end_line(node),
            parent_qual(rel, scope),
            export_kind,
            _column(node),
        )
    )
    call_time_defaults = node.type in {
        "function_declaration",
        "method_definition",
        "generator_function",
        "generator_function_declaration",
    }
    _record_parameters(node, rel, qual, symbols)
    _walk_signature_and_body(
        node,
        rel=rel,
        scope=[*scope, name] if call_time_defaults else scope,
        caller=qual if call_time_defaults else caller,
        enclosing_class=enclosing_class,
        body_scope=[*scope, name],
        body_caller=qual,
        symbols=symbols,
        calls=calls,
        imports=imports,
    )


def _define_function_value(
    node: Node,
    *,
    rel: str,
    scope: list[str],
    caller: str,
    enclosing_class: str,
    in_class_body: bool,
    export_kind: str,
    symbols: list[Symbol],
    calls: list[Call],
    imports: list[Import],
) -> bool:
    value = node.child_by_field_name("value")
    name_node = node.child_by_field_name("name")
    if value is None or value.type not in _FUNC_VALUES or name_node is None:
        return False
    if name_node.type not in _NAME_TYPES:
        return False
    name = _text(name_node)
    if not name:
        return False
    kind = "method" if in_class_body else "function"
    qual = qualified(rel, scope, name)
    symbols.append(
        Symbol(
            rel,
            name,
            qual,
            kind,
            _line(node),
            _end_line(node),
            parent_qual(rel, scope),
            export_kind,
        )
    )
    # JavaScript default parameters run on each call, so they belong to this
    # function. Callers of `run` are then callers of `helper` in
    # `const run = (x = helper()) => x`. Anonymous arrows have no symbol and
    # stay on the enclosing caller.
    _record_parameters(value, rel, qual, symbols)
    _walk_signature_and_body(
        value,
        rel=rel,
        scope=[*scope, name],
        caller=qual,
        enclosing_class=enclosing_class,
        body_scope=[*scope, name],
        body_caller=qual,
        symbols=symbols,
        calls=calls,
        imports=imports,
    )
    return True


def _walk_signature_and_body(
    node: Node,
    *,
    rel: str,
    scope: list[str],
    caller: str,
    enclosing_class: str,
    body_scope: list[str],
    body_caller: str,
    symbols: list[Symbol],
    calls: list[Call],
    imports: list[Import],
) -> None:
    body = node.child_by_field_name("body")
    for child in node.children:
        if body is not None and child == body:
            continue
        _walk(
            child,
            rel=rel,
            scope=scope,
            caller=caller,
            enclosing_class=enclosing_class,
            in_class_body=False,
            export_kind="",
            range_start=None,
            symbols=symbols,
            calls=calls,
            imports=imports,
        )
    if body is not None:
        _walk(
            body,
            rel=rel,
            scope=body_scope,
            caller=body_caller,
            enclosing_class=enclosing_class,
            in_class_body=False,
            export_kind="",
            range_start=None,
            symbols=symbols,
            calls=calls,
            imports=imports,
        )


# Defaults and annotations are not bindings. A later identifier there is a
# use of some other name (`def run(parse=parse())`), not another parameter.
_NON_BINDING = frozenset(
    {
        "type",
        "type_annotation",
        "call",
        "call_expression",
        "attribute",
        "member_expression",
        "string",
        "integer",
        "float",
        "true",
        "false",
        "none",
        "binary_expression",
        "subscript",
        "argument_list",
        "arguments",
    }
)


def _record_parameters(node: Node, rel: str, qual: str, symbols: list[Symbol]) -> None:
    """Record parameter names so they hide imports of the same spelling.

    The qualified name is not a callable symbol. It cannot collide with a
    nested function that reuses the parameter's name.
    """
    seen: set[str] = set()
    for name, line, end in _parameter_spans(node):
        if not name or name in seen:
            continue
        seen.add(name)
        symbols.append(Symbol(rel, name, f"{qual}::param:{name}", "parameter", line, end, qual, ""))


def _parameter_spans(node: Node) -> list[tuple[str, int, int]]:
    params = node.child_by_field_name("parameters")
    if params is None:
        return _bare_arrow_parameter(node)
    found: list[tuple[str, int, int]] = []
    for child in params.named_children:
        found.extend(_pattern_names(child))
    return found


def _bare_arrow_parameter(node: Node) -> list[tuple[str, int, int]]:
    """`parse => parse()` has no parameter list, only a bare identifier."""
    if node.type != "arrow_function":
        return []
    named = node.child_by_field_name("parameter")
    if named is not None:
        return _pattern_names(named)
    for child in node.children:
        if child.type == "=>" or _text(child) == "=>":
            break
        if child.type == "identifier":
            return _pattern_names(child)
    return []


def _pattern_names(node: Node) -> list[tuple[str, int, int]]:
    if node.type in {"identifier", "shorthand_property_identifier_pattern"}:
        text = _text(node)
        if not text:
            return []
        return [(text, _line(node), _end_line(node))]
    if node.type in _NON_BINDING:
        return []
    if node.type in {"object_pattern", "array_pattern"}:
        return _destructured_names(node)
    if node.type in {"tuple_pattern", "list_pattern", "pattern_list"}:
        found: list[tuple[str, int, int]] = []
        for child in node.named_children:
            found.extend(_pattern_names(child))
        return found
    for child in node.named_children:
        if child.type in _NON_BINDING:
            break
        names = _pattern_names(child)
        if names:
            return names
    return []


def _destructured_names(node: Node) -> list[tuple[str, int, int]]:
    """Names a TypeScript object or array pattern binds, defaults excluded."""
    found: list[tuple[str, int, int]] = []
    for child in node.named_children:
        if child.type == "pair_pattern":
            value = child.child_by_field_name("value")
            if value is not None:
                found.extend(_binding_target(value))
        else:
            found.extend(_binding_target(child))
    return found


def _binding_target(node: Node) -> list[tuple[str, int, int]]:
    if node.type in {"object_assignment_pattern", "assignment_pattern"}:
        left = node.child_by_field_name("left")
        return _binding_target(left) if left is not None else []
    if node.type == "rest_pattern":
        return [name for child in node.named_children for name in _binding_target(child)]
    return _pattern_names(node)


def _peek_qual(node: Node, rel: str, scope: list[str], in_class_body: bool) -> str:
    del in_class_body
    name = _node_name(node)
    if not name:
        return ""
    return qualified(rel, scope, name)


def _record_call(
    node: Node, rel: str, caller: str, enclosing_class: str, calls: list[Call]
) -> None:
    func = node.child_by_field_name("function")
    if func is None:
        return
    _record_callee_node(func, rel, caller, enclosing_class, calls, _line(node))


def _record_callee_node(
    func: Node,
    rel: str,
    caller: str,
    enclosing_class: str,
    calls: list[Call],
    line: int,
) -> None:
    receiver, name, kind = _split_callee(func)
    if not name:
        return
    calls.append(
        Call(
            path=rel,
            caller=caller,
            callee=_text(func)[:180],
            callee_name=name,
            receiver=receiver[:180],
            receiver_kind=kind,
            line=line,
            enclosing_class=enclosing_class,
            column=_column(func),
        )
    )


def _record_jsx(
    node: Node, rel: str, caller: str, enclosing_class: str, calls: list[Call]
) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    if name_node.type == "identifier":
        name = _text(name_node)
        if not name or name[0].islower():
            return
        calls.append(
            Call(rel, caller, name, name, "", "", _line(name_node), enclosing_class, _column(name_node))
        )
        return
    if name_node.type in _MEMBER_TYPES or name_node.type == "nested_identifier":
        receiver, name, kind = _split_callee(name_node)
        if name and not name[0].islower():
            calls.append(
                Call(
                    rel,
                    caller,
                    _text(name_node)[:180],
                    name,
                    receiver[:180],
                    kind,
                    _line(name_node),
                    enclosing_class,
                )
            )


def _split_callee(node: Node) -> tuple[str, str, str]:
    if node.type in _NAME_TYPES:
        return "", _text(node), ""
    if node.type not in _MEMBER_TYPES and node.type != "nested_identifier":
        return "", "", ""
    obj = node.child_by_field_name("object")
    attr = node.child_by_field_name("attribute") or node.child_by_field_name("property")
    if attr is None:
        for child in node.named_children:
            if child.type in _NAME_TYPES:
                attr = child
    name = _text(attr)
    if not name or obj is None:
        return "", name, ""
    if obj.type == "identifier":
        receiver = _text(obj)
        if receiver in {"self", "cls"}:
            return receiver, name, receiver
        return receiver, name, "ident"
    if obj.type == "this":
        return "this", name, "this"
    if obj.type == "super":
        return "super", name, "this"
    if obj.type in _CALL_TYPES:
        inner = obj.child_by_field_name("function")
        if inner is not None and inner.type == "identifier":
            return _text(inner), name, "ctor"
        return _text(obj)[:180], name, "ctor"
    if obj.type == "new_expression":
        ctor = obj.child_by_field_name("constructor")
        if ctor is not None and ctor.type in _NAME_TYPES:
            return _text(ctor), name, "ctor"
        return _text(ctor)[:180], name, "ctor"
    if obj.type in _MEMBER_TYPES:
        return _text(obj)[:180], name, "attr"
    return _text(obj)[:180], name, "other"


def _looks_like_python_import(node: Node) -> bool:
    return any(child.type in {"dotted_name", "aliased_import"} for child in node.named_children)


def _python_import(node: Node, rel: str, scope: str) -> list[Import]:
    found: list[Import] = []
    line = _line(node)
    for child in node.named_children:
        if child.type == "dotted_name":
            module = _text(child)
            # `import a.b` binds the name `a` to package a. `a.foo()` is a.foo.
            # `import a.b as b` is handled below and binds b to module a.b.
            root = module.split(".", 1)[0]
            found.append(Import(rel, root, "", root, "module", line, 0, scope, _column(node)))
            if module != root:
                found.append(Import(rel, module, "", "", "module", line, 0, scope, _column(node)))
        elif child.type == "aliased_import":
            module = _text(child.child_by_field_name("name"))
            alias = _text(child.child_by_field_name("alias"))
            found.append(Import(rel, module, "", alias or module.split(".", 1)[0], "module", line, 0, scope, _column(node)))
    return found


def _python_from(node: Node, rel: str, scope: str) -> list[Import]:
    module_node = node.child_by_field_name("module_name")
    level = 0
    module = ""
    if module_node is not None and module_node.type == "relative_import":
        for child in module_node.children:
            if child.type == "import_prefix":
                level = len(_text(child))
            elif child.type == "dotted_name":
                module = _text(child)
    elif module_node is not None:
        module = _text(module_node)
    found: list[Import] = []
    line = _line(node)
    for child in node.named_children:
        if child == module_node:
            continue
        if child.type == "dotted_name":
            name = _text(child)
            found.append(Import(rel, module, name, name, "from", line, level, scope, _column(node)))
        elif child.type == "aliased_import":
            name = _text(child.child_by_field_name("name"))
            alias = _text(child.child_by_field_name("alias")) or name
            found.append(Import(rel, module, name, alias, "from", line, level, scope, _column(node)))
        elif child.type == "wildcard_import":
            found.append(Import(rel, module, "*", "*", "star", line, level, scope, _column(node)))
    return found


def _ts_import(node: Node, rel: str, scope: str) -> list[Import]:
    module = _unquote(node.child_by_field_name("source"))
    line = _line(node)
    type_only = any(_is_keyword(child, "type") for child in node.children)
    clause = next((child for child in node.named_children if child.type == "import_clause"), None)
    if clause is None:
        kind = "type" if type_only else "side_effect"
        return [Import(rel, module, "", "", kind, line, 0, scope, _column(node))]
    found: list[Import] = []
    for child in clause.named_children:
        if child.type == "identifier":
            kind = "type" if type_only else "default"
            found.append(Import(rel, module, "default", _text(child), kind, line, 0, scope, _column(node)))
        elif child.type == "namespace_import":
            alias = next((_text(item) for item in child.named_children if item.type == "identifier"), "")
            kind = "type" if type_only else "namespace"
            found.append(Import(rel, module, "*", alias, kind, line, 0, scope, _column(node)))
        elif child.type == "named_imports":
            for spec in child.named_children:
                if spec.type != "import_specifier":
                    continue
                spec_type = type_only or any(_is_keyword(item, "type") for item in spec.children)
                name = _text(spec.child_by_field_name("name"))
                alias_node = spec.child_by_field_name("alias")
                alias = _text(alias_node) if alias_node is not None else name
                kind = "type" if spec_type else "named"
                found.append(Import(rel, module, name, alias, kind, line, 0, scope, _column(node)))
    return found
