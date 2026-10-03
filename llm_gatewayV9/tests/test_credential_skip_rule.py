"""The credential-skip rule: env failures skip, real failures stay red.

A gate that skips everything is as useless as one that is always red, so
both directions are asserted. Extracted by AST from test_deployment.py
rather than importing it, because that module probes a live gateway at import
time.
"""
import ast
import pathlib
import textwrap

import pytest

SRC = pathlib.Path(
    r"C:\Users\AISHWARYA\Downloads\project3\llm_gatewayV9\tests\test_deployment.py")
tree = ast.parse(SRC.read_text(encoding="utf-8"))
wanted = {"_CRED_ENV_ERRORS", "cred_env_or_skip"}
ns = {"pytest": pytest}
for node in tree.body:
    name = getattr(node, "name", None)
    if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) in wanted:
        exec(compile(ast.Module([node], []), "<dep>", "exec"), ns)
    if isinstance(node, ast.FunctionDef) and name in wanted:
        exec(compile(ast.Module([node], []), "<dep>", "exec"), ns)

cred_env_or_skip = ns["cred_env_or_skip"]


@pytest.mark.parametrize("err", [
    "HTTPStatusError: Client error '401 Unauthorized' for url",
    "invalid_auth",
    "invalid_grant: token has been expired",
    "not configured",
    "revoked token",
    "403 forbidden",
    "GMAIL_API_KEY missing",
])
def test_environment_failures_skip(err):
    with pytest.raises(pytest.skip.Exception) as ei:
        cred_env_or_skip("svc", {"ok": False, "error": err})
    assert "env/creds" in str(ei.value), ei.value


def test_success_never_skips_or_raises():
    assert cred_env_or_skip("svc", {"ok": True}) is None


@pytest.mark.parametrize("err", [
    "500 Internal Server Error",
    "AttributeError: 'NoneType' object has no attribute 'json'",
    "connection reset by peer",
    "",
])
def test_real_failures_stay_red(err):
    with pytest.raises(AssertionError):
        cred_env_or_skip("svc", {"ok": False, "error": err})
