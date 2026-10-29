"""Prove an instrumented strategy only adds TemperQ recording and changes nothing else.

The skill (or a user) adds `temperq_record(...)` calls and the recorder block to a copy of the strategy
so a re-run leaves daily scores. This compares the two files' syntax trees after removing exactly those
additions: if anything else differs, the copy is rejected and the differing lines are named.
Code is parsed, never run.
"""
from __future__ import annotations

import ast

RECORDER_NAMES = {"temperq_record", "_temperq_write", "TEMPERQ_FILE", "_temperq_rows"}


def _is_recorder(node):
    """A statement that belongs to the recorder: its definitions, or a bare temperq_record(...) call."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in RECORDER_NAMES:
        return True
    if isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) and t.id in RECORDER_NAMES for t in node.targets):
        return True
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        f = node.value.func
        return isinstance(f, ast.Name) and f.id == "temperq_record"
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) \
            and "TemperQ 记录器" in node.value.value:
        return True
    return False


class _Strip(ast.NodeTransformer):
    def generic_visit(self, node):
        super().generic_visit(node)
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list):
                kept = [n for n in block if not _is_recorder(n)]
                setattr(node, field, kept or [ast.Pass()])
        return node


def _tree(source):
    from .code_parse import parse
    tree, _ = parse(source)
    if tree is None:
        raise ValueError("代码无法解析")
    return tree


def _statements(tree):
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt) and not isinstance(node, (ast.Module,)):
            out.append(node)
    return out


def compare(original, instrumented):
    """{'same_logic': bool, 'added_calls': int, 'differences': [...]}"""
    a, b = _tree(original), _tree(instrumented)
    added = sum(1 for n in ast.walk(b) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "temperq_record")
    sa = _Strip().visit(a)
    sb = _Strip().visit(b)
    same = ast.dump(sa, include_attributes=False) == ast.dump(sb, include_attributes=False)
    differences = []
    if not same:
        la = [ast.dump(n, include_attributes=False) for n in _statements(sa)]
        lb = _statements(sb)
        seen = set(la)
        for node in lb:
            if ast.dump(node, include_attributes=False) not in seen and not isinstance(node, ast.Pass):
                differences.append({"line": getattr(node, "lineno", None),
                                    "code": ast.unparse(node).splitlines()[0][:160]})
            if len(differences) >= 10:
                break
    return {"same_logic": same, "added_calls": added, "differences": differences,
            "meaning": ("除 TemperQ 记录语句外，两份代码的语法树完全相同：选股和下单逻辑没有改动" if same else
                        "除记录语句外还有改动，这份副本不能用于审计；请只添加 temperq_record 调用和记录器代码")}
