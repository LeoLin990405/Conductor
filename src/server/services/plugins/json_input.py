"""Cheap depth guard for untrusted JSON documents."""

MAX_JSON_NESTING = 128


def exceeds_json_nesting_limit(raw: bytes | str, limit: int = MAX_JSON_NESTING) -> bool:
    """Return whether JSON structural nesting exceeds ``limit``.

    This scans strings and escapes without parsing the document. The regular
    JSON parser still owns syntax validation; this guard only bounds recursive
    work in decoders, validators, and model adapters.
    """
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > limit:
                return True
        elif char in "]}":
            depth = max(0, depth - 1)
    return False
