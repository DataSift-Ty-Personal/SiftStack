"""The model file is the only place a model is named (src/model_registry.py). Zero network.

Ty, 2026-11-17: "make sure that all of the workflows use these proven models." This test is how: a model id
written as a string anywhere else in src/ (docstrings and comments aside) or in team/*.yaml fails it, so no
job can drift back to an old default or onto an untested model without the model file saying so.
"""
import ast
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import model_registry  # noqa: E402

MODEL_ID = re.compile(
    r"^(claude-(opus|sonnet|haiku|fable|mythos)-\d[\w.-]*"
    r"|(anthropic|openai|deepseek|meta|google|moonshotai|qwen|z-ai|xiaomi|x-ai|mistralai)/[\w.:-]+)$")
# The model file itself, and the selftests, whose fakes name models on purpose.
ALLOWED = {"model_registry.py"}


def _docstring_nodes(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                out.add(id(body[0].value))
    return out


def model_literals(path: Path):
    """(line, text) for every string constant in a file that is a model id, docstrings excepted."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    docs = _docstring_nodes(tree)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            if MODEL_ID.match(node.value.strip()):
                hits.append((node.lineno, node.value))
    return hits


def scan_src():
    found = []
    for p in sorted(SRC.rglob("*.py")):
        if "__pycache__" in p.parts or p.name in ALLOWED or p.name.startswith("selftest"):
            continue
        for line, text in model_literals(p):
            found.append("%s:%d %s" % (p.relative_to(ROOT).as_posix(), line, text))
    return found


def scan_team_yaml():
    found = []
    for p in sorted((ROOT / "team").glob("*.yaml")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            for tok in re.findall(r"[\w./:-]+", line.split("#", 1)[0]):
                if MODEL_ID.match(tok):
                    found.append("%s:%d %s" % (p.relative_to(ROOT).as_posix(), i, tok))
    return found


def test_no_model_named_outside_the_model_file():
    found = scan_src() + scan_team_yaml()
    assert not found, "model ids written outside src/model_registry.py:\n  " + "\n  ".join(found)


def test_every_job_is_complete():
    for name, j in model_registry.JOBS.items():
        for key in ("model", "env", "via", "what", "proof"):
            assert j.get(key), "job %s has no %s" % (name, key)
        assert j["via"] in ("anthropic", "openrouter"), name
        assert MODEL_ID.match(j["model"]), "job %s names %r, which is not a model id" % (name, j["model"])
        # Claude API ids are bare; OpenRouter ids are vendor/name. A mix-up 404s at the first call.
        assert ("/" in j["model"]) == (j["via"] == "openrouter"), "job %s: %s does not fit via=%s" % (
            name, j["model"], j["via"])


def test_every_claude_api_model_has_a_price():
    for name, j in model_registry.JOBS.items():
        if j["via"] == "anthropic":
            assert j["model"] in model_registry.PRICES, "no price for %s (job %s)" % (j["model"], name)


def test_a_box_setting_moves_one_job_only(monkeypatch=None):
    env = model_registry.JOBS["text_replies"]["env"]
    keep = os.environ.get(env)
    try:
        os.environ[env] = "claude-sonnet-5-5"
        assert model_registry.model("text_replies") == "claude-sonnet-5-5"
        assert model_registry.overridden("text_replies")
        assert model_registry.model("call_grading") == model_registry.default("call_grading")
    finally:
        if keep is None:
            os.environ.pop(env, None)
        else:
            os.environ[env] = keep
    assert model_registry.model("text_replies") == model_registry.default("text_replies")


def test_unknown_job_fails_loudly():
    try:
        model_registry.model("no-such-job")
    except KeyError as e:
        assert "no model job" in str(e)
    else:
        raise AssertionError("an unknown job must raise")


def test_thinking_off_per_family():
    assert model_registry.thinking_off("claude-haiku-5-5") == {"type": "disabled"}
    assert model_registry.thinking_off("claude-sonnet-5-5") == {"type": "between_tools"}
    assert model_registry.thinking_off("claude-opus-5-5") is None
    assert model_registry.thinking_off("claude-haiku-4-5-20251001") is None
    assert not model_registry.server_fallback("claude-haiku-5-5")
    assert model_registry.server_fallback("claude-opus-5-5")


def test_the_scanner_catches_a_literal(tmp_path=None):
    import tempfile
    d = Path(tempfile.mkdtemp())
    f = d / "x.py"
    f.write_text('"""A docstring may name claude-opus-5-5."""\nMODEL = "claude-opus-5-5"\n', encoding="utf-8")
    hits = model_literals(f)
    assert hits == [(2, "claude-opus-5-5")], hits


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok   ", name)
            except AssertionError as e:
                fails += 1
                print("FAIL ", name, e)
    sys.exit(1 if fails else 0)
