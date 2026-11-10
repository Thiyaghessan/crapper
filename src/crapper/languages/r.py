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

The namespace is the file path with the leading `R/` folder and the `.R`
suffix removed, dotted: `R/transforms/dates.R` is `transforms.dates`.
"""

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


def _called_name(data: bytes, call) -> str:
    """Name of the function a call names: `case_when` for `dplyr::case_when(...)`."""

    function = call.child_by_field_name("function")
    if function is None:
        return ""
    if function.type == "namespace_operator":
        function = function.child_by_field_name("rhs")
    if function is None or function.type != "identifier":
        return ""
    return node_text(data, function)


def _enclosing_call(argument):
    """The `name(...)` call an argument belongs to, or None for `x[...]`."""

    arguments = argument.parent
    if arguments is None or arguments.parent is None:
        return None
    if arguments.parent.type != "call":
        return None
    return arguments.parent


def _argument_name(data: bytes, argument) -> str:
    name = argument.child_by_field_name("name")
    if name is None:
        return ""
    return node_text(data, name)


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


def _is_case_when_condition(value) -> bool:
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


def _strip_r_folder(relative: str) -> str:
    if relative.startswith("./"):
        relative = relative[2:]
    if relative.startswith("R/"):
        return relative[2:]
    if "/R/" in relative:
        return relative.split("/R/", 1)[1]
    return relative


def _file_namespace(path: str) -> str:
    relative = _strip_r_folder(path.replace("\\", "/"))
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
    if name is None or name.type != "identifier":
        return None
    if function is None or function.type != "function_definition":
        return None
    return name, function


def functions_in_source(
    source: str, path: str, project_root: str | None = None
) -> list[Function]:
    data, tree = parse(source, "r")
    namespace = _file_namespace(path)

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
                name=node_text(data, name),
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
