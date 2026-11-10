import shutil
import subprocess
from pathlib import Path

import pytest

from crapper.coverage import CoverageBundle, parse_lcov
from crapper.discover import is_test_file, language_of
from crapper.languages.r import functions_in_source
from crapper.runners import (
    r_coverage_command,
    r_roots,
    r_test_files,
    run_coverage,
)


def _write(root: Path, relative: str, source: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _scores(source: str, path: str = "R/demo.R") -> list[tuple[str, str, int]]:
    functions = functions_in_source(source, path, "/proj")
    return [(fn.namespace, fn.name, fn.complexity) for fn in functions]


def test_both_assignment_forms_are_found():
    source = """
arrow <- function(x) x
equals = function(x) x
global <<- function(x) x
not_a_function <- 3
"""
    assert _scores(source) == [
        ("demo", "arrow", 1),
        ("demo", "equals", 1),
        ("demo", "global", 1),
    ]


def test_if_else_if_and_loops_each_count_once():
    source = """
choose <- function(x, ready) {
  if (x > 1) {
    return(1)
  } else if (x == 0) {
    return(0)
  } else {
    x <- 2
  }
  for (i in 1:3) next
  while (ready) break
  repeat {
    break
  }
  return(x)
}
"""
    # 1 + if + else if + for + while + repeat
    assert _scores(source) == [("demo", "choose", 6)]


def test_short_circuit_logic_counts_and_vectorised_logic_does_not():
    source = """
both <- function(x, y) {
  if (x && y || !x) 1 else 0
}
elementwise <- function(x, y) {
  return(x & y | !x)
}
"""
    assert _scores(source) == [("demo", "both", 4), ("demo", "elementwise", 1)]


def test_switch_counts_each_alternative_that_has_a_value():
    source = """
pick <- function(kind) {
  switch(kind,
    a = 1,
    b = ,
    c = 2,
    3
  )
}
"""
    # 1 + a + c + the default. `b = ,` falls through to c and has no code of its own.
    assert _scores(source) == [("demo", "pick", 4)]


def test_trycatch_counts_each_handler_and_not_finally():
    source = """
guarded <- function(x) {
  tryCatch(
    risky(x),
    error = function(e) NULL,
    warning = function(w) NA,
    finally = cleanup()
  )
}
named_expression <- function(x) {
  tryCatch(expr = risky(x), error = function(e) NULL)
}
"""
    assert _scores(source) == [("demo", "guarded", 3), ("demo", "named_expression", 2)]


def test_case_when_counts_once_per_condition():
    source = """
label <- function(x) {
  dplyr::case_when(
    x > 1 ~ "big",
    x < 0 ~ "negative",
    TRUE ~ "small"
  )
}
bare <- function(x) {
  case_when(x > 1 ~ "big", .default = "small")
}
"""
    assert _scores(source) == [("demo", "label", 4), ("demo", "bare", 2)]


def test_nested_and_anonymous_functions_stay_inside_the_enclosing_function():
    source = """
outer <- function(xs) {
  inner <- function(x) {
    if (x) 1 else 0
  }
  purrr::map(xs, function(x) if (x) inner(x) else 0)
}
handlers <- list(on_error = function(e) if (TRUE) 1 else 0)
"""
    assert _scores(source) == [("demo", "outer", 3)]


def test_strings_and_comments_do_not_add_decisions():
    source = """
literal <- function() {
  # if (x) for while && ||
  text <- "if (x && y) switch(x, a = 1)"
  return(text)
}
"""
    assert _scores(source) == [("demo", "literal", 1)]


def test_namespace_is_the_file_path_without_the_r_folder_or_suffix():
    source = "f <- function() 1\n"
    assert _scores(source, "R/transforms/dates.R")[0][0] == "transforms.dates"
    assert _scores(source, "pkg/R/dates.r")[0][0] == "dates"
    assert _scores(source, "scripts/build.R")[0][0] == "scripts.build"


def test_lines_and_bytes_span_the_whole_assignment():
    source = "\nf <- function(x) {\n  x\n}\n"
    function = functions_in_source(source, "R/demo.R", "/proj")[0]
    assert (function.start_line, function.end_line) == (2, 4)
    assert source.encode("utf-8")[function.start_byte : function.end_byte].decode("utf-8") == (
        "f <- function(x) {\n  x\n}"
    )
    assert function.language == "r"


def test_r_files_are_recognised_and_test_files_are_left_out():
    assert language_of("R/build.R") == "r"
    assert language_of("R/build.r") == "r"
    assert is_test_file("test_build.R") is True
    assert is_test_file("test-build.R") is True
    assert is_test_file("test-build.r") is True
    assert is_test_file("R/build.R") is False
    assert is_test_file("R/testing_helpers.R") is False


def test_an_r_package_is_measured_with_package_coverage(tmp_path):
    _write(tmp_path, "DESCRIPTION", "Package: demo\n")
    source = _write(tmp_path, "R/box.R", "f <- function() 1\n")
    assert r_roots(tmp_path, [source]) == [tmp_path.resolve()]
    report = tmp_path / "target" / "coverage" / "r" / "lcov.info"
    command = r_coverage_command(tmp_path, [source], report)
    assert command[:2] == ["Rscript", "-e"]
    assert "covr::package_coverage()" in command[2]
    assert "covr::file_coverage" not in command[2]
    assert command[3:] == [str(report)]


def test_loose_r_scripts_are_measured_with_file_coverage(tmp_path):
    source = _write(tmp_path, "R/box.R", "f <- function() 1\n")
    other = _write(tmp_path, "src/app.py", "def run():\n    return 1\n")
    test = _write(tmp_path, "tests/test_box.R", "stopifnot(f() == 1)\n")
    _write(tmp_path, "tests/run_all.R", "source('tests/test_box.R')\n")
    assert r_test_files(tmp_path) == [test]
    report = tmp_path / "target" / "coverage" / "r" / "lcov.info"
    command = r_coverage_command(tmp_path, [source, other, test], report)
    assert "covr::file_coverage" in command[2]
    assert "covr::package_coverage" not in command[2]
    assert command[3:] == [str(report), "R/box.R", "--tests", "tests/test_box.R"]


def test_testthat_files_are_preferred_when_they_exist(tmp_path):
    _write(tmp_path, "tests/test_old.R", "")
    testthat = _write(tmp_path, "tests/testthat/test-box.R", "")
    assert r_test_files(tmp_path) == [testthat]


def test_missing_covr_is_reported_and_nothing_else_runs(tmp_path, monkeypatch, capsys):
    seen = []

    def shell(command, cwd):
        seen.append(command)
        return 1

    monkeypatch.setattr("crapper.runners.run_shell", shell)
    source = _write(tmp_path, "R/box.R", "f <- function() 1\n")
    _write(tmp_path, "tests/test_box.R", "")
    run_coverage(tmp_path, [source], None)
    assert len(seen) == 1
    assert "covr is not installed" in capsys.readouterr().err


def test_r_without_tests_is_skipped(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("crapper.runners.run_shell", lambda *_args: 0)
    source = _write(tmp_path, "R/box.R", "f <- function() 1\n")
    run_coverage(tmp_path, [source], None)
    assert "No R test files" in capsys.readouterr().err


def test_a_failed_r_run_is_reported(tmp_path, monkeypatch, capsys):
    codes = iter([0, 3])
    monkeypatch.setattr("crapper.runners.run_shell", lambda *_args: next(codes))
    source = _write(tmp_path, "R/box.R", "f <- function() 1\n")
    _write(tmp_path, "tests/test_box.R", "")
    run_coverage(tmp_path, [source], None)
    assert "R coverage exited 3" in capsys.readouterr().err


def test_lcov_written_for_r_scores_a_function_by_its_lines():
    lcov = parse_lcov("SF:R/box.R\nDA:2,4\nDA:3,0\nDA:7,1\nend_of_record\n")
    function = functions_in_source("\nf <- function(x) {\n  x\n}\n", "R/box.R", "/proj")[0]
    assert CoverageBundle(lcov=lcov).percent_for(function) == 50.0


def _covr_is_installed() -> bool:
    if shutil.which("Rscript") is None:
        return False
    check = subprocess.run(
        ["Rscript", "-e", "library(covr)"], capture_output=True, check=False
    )
    return check.returncode == 0


@pytest.mark.skipif(not _covr_is_installed(), reason="needs R with the covr package")
def test_covr_really_writes_lcov_that_crapper_reads(tmp_path):
    source = _write(
        tmp_path,
        "R/box.R",
        "sign_of <- function(x) {\n"
        "  if (x > 0) {\n"
        "    return(1)\n"
        "  }\n"
        "  return(-1)\n"
        "}\n",
    )
    # The test file calls quit(), as a standalone R test script often does.
    _write(tmp_path, "tests/test_box.R", "stopifnot(sign_of(5) == 1)\nquit(status = 0L)\n")
    report = tmp_path / "target" / "coverage" / "r" / "lcov.info"
    report.parent.mkdir(parents=True)
    command = r_coverage_command(tmp_path, [source], report)
    done = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    lcov = parse_lcov(report.read_text(encoding="utf-8"))
    function = functions_in_source(source.read_text(encoding="utf-8"), "R/box.R", str(tmp_path))[0]
    # Lines 2 and 3 ran; line 5 did not.
    assert round(CoverageBundle(lcov=lcov).percent_for(function), 1) == 66.7
