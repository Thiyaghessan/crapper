"""R functions and cyclomatic complexity.

A function is an entry when it is assigned to a plain name with `<-`, `=`,
or `<<-` and is not inside another function. A function defined inside
another function, or passed as an argument (a `purrr::map` callback, a
`tryCatch` handler, a member of a `list(...)`), stays inside the enclosing
entry, as nested functions do in Python.

Decision points, one each:

- `if`. An `else if` is an `if` inside an `else`, so it counts as its own `if`.
- `for`, `while`, `repeat`.
- `&&` and `||`. The vectorised `&` and `|` work on whole columns and do not
  choose a path, so they do not count.
- Each alternative of `switch(...)` that has a value, the unnamed default
  included. A label with no value (`b = ,`) falls through to the next
  alternative and has no code of its own, so it does not count.
- Each handler of `tryCatch(...)`: every named argument except `expr` and
  `finally`, which are not handlers.
- Each condition of `case_when(...)`: every `condition ~ value` argument,
  the `TRUE ~ value` catch-all included. `.default = value` is not a
  condition.

A name means the same however it is written: `error`, `"error"`, and
`` `error` `` are one name, for a function, a called function, and an
argument. An escape in a name is read as R reads it. A `case_when` condition counts with
or without parentheses round it.

The namespace is the file path inside the project, with the leading `R/`
folder and the `.R` suffix removed, dotted: `R/transforms/dates.R` is
`transforms.dates`, and `scripts/build.R` is `scripts.build`.
"""

import re

from crapper.languages.treesitter import (
    complexity,
    descendants,
    end_line,
    node_text,
    parse,
    start_line,
)
from crapper.model import Function

_ASSIGNMENTS = {"<-", "=", "<<-"}
_BRANCHING_STATEMENTS = {
    "if_statement",
    "for_statement",
    "while_statement",
    "repeat_statement",
}
_SHORT_CIRCUIT = {"&&", "||"}
_NOT_TRYCATCH_HANDLERS = {"expr", "finally"}


def _operator(node) -> str:
    operator = node.child_by_field_name("operator")
    if operator is None:
        return ""
    return operator.type


# How R writes a character it cannot or need not type. \u0041, \u{41} and
# \U00000041 name a character. \x41 and \101 name one byte, and a character
# outside ASCII takes several of them: \xc3\xa9 is the two bytes of e-acute.
# A backslash in front of anything else gives that character, or the control
# character the letter stands for (\n, \t).
_ESCAPE = re.compile(
    r"\\(?:u\{([0-9a-fA-F]{1,4})\}|u([0-9a-fA-F]{1,4})"
    r"|U\{([0-9a-fA-F]{1,8})\}|U([0-9a-fA-F]{1,8})"
    r"|x([0-9a-fA-F]{1,2})|([0-7]{1,3})|(.))",
    re.DOTALL,
)
_LETTER_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}


def _escape_bytes(match) -> bytes:
    """The bytes one escape stands for."""

    *character_codes, hex_byte, octal_byte, letter = match.groups()
    for digits in character_codes:
        if digits is not None:
            return chr(int(digits, 16)).encode("utf-8")
    if hex_byte is not None:
        return bytes([int(hex_byte, 16)])
    if octal_byte is not None:
        return bytes([int(octal_byte, 8) & 0xFF])
    return _LETTER_ESCAPES.get(letter, letter).encode("utf-8")


def _unescaped(text: str) -> str:
    """`text` with each R escape replaced by what it stands for.

    The result is built as bytes and read as UTF-8 at the end, because one
    character can be written as several byte escapes in a row.
    """

    decoded = bytearray()
    position = 0
    for match in _ESCAPE.finditer(text):
        decoded += text[position : match.start()].encode("utf-8")
        decoded += _escape_bytes(match)
        position = match.end()
    decoded += text[position:].encode("utf-8")
    return decoded.decode("utf-8", errors="replace")


def _string_value(data: bytes, node) -> str:
    content = node.child_by_field_name("content")
    if content is None:
        return ""
    text = node_text(data, content)
    opening = node_text(data, node.child_by_field_name("open"))
    if opening[:1] in {"r", "R"}:
        # A raw string, r"(...)", has no escapes.
        return text
    return _unescaped(text)


def plain_name(data: bytes, node) -> str:
    """A name as R reads it, whatever way it is written.

    `error`, `"error"`, and `` `error` `` are the same name, and so is a
    spelling with an escape in it. Anything that is not a name or a string
    has no plain name and gives "".
    """

    if node is None:
        return ""
    if node.type == "identifier":
        text = node_text(data, node)
        if text.startswith("`"):
            # Take off the one backtick at each end. A name may end in an
            # escaped backtick of its own.
            return _unescaped(text[1:-1])
        return text
    if node.type == "string":
        return _string_value(data, node)
    return ""


def _called_name(data: bytes, call) -> str:
    """Name of the function a call names: `case_when` for `dplyr::case_when(...)`."""

    function = call.child_by_field_name("function")
    if function is not None and function.type == "namespace_operator":
        function = function.child_by_field_name("rhs")
    return plain_name(data, function)


def _enclosing_call(argument):
    """The `name(...)` call an argument belongs to, or None for `x[...]`."""

    arguments = argument.parent
    if arguments is None or arguments.parent is None:
        return None
    if arguments.parent.type != "call":
        return None
    return arguments.parent


def _argument_name(data: bytes, argument) -> str:
    return plain_name(data, argument.child_by_field_name("name"))


def _is_first_argument(argument) -> bool:
    for sibling in argument.parent.children:
        if sibling.type == "argument":
            return sibling.id == argument.id
    return False


def _is_switch_alternative(argument, value) -> bool:
    return value is not None and not _is_first_argument(argument)


def _is_trycatch_handler(data: bytes, argument, value) -> bool:
    name = _argument_name(data, argument)
    return value is not None and name != "" and name not in _NOT_TRYCATCH_HANDLERS


def _without_parentheses(node):
    while node is not None and node.type == "parenthesized_expression":
        node = node.child_by_field_name("body")
    return node


def _is_case_when_condition(value) -> bool:
    value = _without_parentheses(value)
    return value is not None and value.type == "binary_operator" and _operator(value) == "~"


def _argument_is_decision(data: bytes, argument) -> bool:
    call = _enclosing_call(argument)
    if call is None:
        return False
    called = _called_name(data, call)
    value = argument.child_by_field_name("value")
    if called == "switch":
        return _is_switch_alternative(argument, value)
    if called == "tryCatch":
        return _is_trycatch_handler(data, argument, value)
    if called == "case_when":
        return _is_case_when_condition(value)
    return False


def _is_decision(data: bytes, node) -> bool:
    if node.type in _BRANCHING_STATEMENTS:
        return True
    if node.type == "binary_operator":
        return _operator(node) in _SHORT_CIRCUIT
    if node.type == "argument":
        return _argument_is_decision(data, node)
    return False


def _skip(_node) -> bool:
    return False


def _inside_project(path: str, project_root: str | None) -> str:
    """The path from the project root down, so a full path and a short one agree."""

    root = (project_root or "").replace("\\", "/").rstrip("/")
    if root and path.startswith(root + "/"):
        return path[len(root) + 1 :]
    return path


def _strip_r_folder(relative: str) -> str:
    if relative.startswith("./"):
        relative = relative[2:]
    if relative.startswith("R/"):
        return relative[2:]
    if "/R/" in relative:
        return relative.split("/R/", 1)[1]
    return relative


def _file_namespace(path: str, project_root: str | None) -> str:
    relative = _strip_r_folder(_inside_project(path.replace("\\", "/"), project_root))
    if relative.lower().endswith(".r"):
        relative = relative[:-2]
    return relative.strip("/").replace("/", ".")


def _is_inside_function(node) -> bool:
    current = node.parent
    while current is not None:
        if current.type == "function_definition":
            return True
        current = current.parent
    return False


def _named_function(node):
    """The (name node, function node) of `name <- function(...)`, or None."""

    if node.type != "binary_operator" or _operator(node) not in _ASSIGNMENTS:
        return None
    name = node.child_by_field_name("lhs")
    function = node.child_by_field_name("rhs")
    if name is None or name.type not in {"identifier", "string"}:
        return None
    if function is None or function.type != "function_definition":
        return None
    return name, function


def functions_in_source(
    source: str, path: str, project_root: str | None = None
) -> list[Function]:
    data, tree = parse(source, "r")
    namespace = _file_namespace(path, project_root)

    def is_decision(node) -> bool:
        return _is_decision(data, node)

    found: list[Function] = []
    for node in descendants(tree.root_node):
        named = _named_function(node)
        if named is None or _is_inside_function(node):
            continue
        name, function = named
        found.append(
            Function(
                name=plain_name(data, name),
                namespace=namespace,
                complexity=complexity(function, is_decision, _skip),
                start_line=start_line(node),
                end_line=end_line(node),
                path=path,
                language="r",
                start_byte=node.start_byte,
                end_byte=node.end_byte,
            )
        )
    return found
