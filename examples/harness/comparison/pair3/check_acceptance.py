"""Behavior plus actual reuse; an unchanged duplicate cannot pass."""
import ast
import importlib.util
from pathlib import Path
import sys
arm = sys.argv[1]
assert arm in {"direct", "delegated"}
p = Path(__file__).parent / arm / "labels.py"
spec = importlib.util.spec_from_file_location("labels", p)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
for value, expected in [(True, False), (False, False), (None, False), ("2", False), (0, False), (-1, False), (1, True), (3, True), (1.0, False)]:
    assert m.is_positive_int(value) is expected
    assert m.is_positive_count(value) is expected
if m.is_positive_count is not m.is_positive_int:
    tree = ast.parse(p.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "is_positive_count")
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "is_positive_int" for n in ast.walk(fn)), "count validator must reuse the integer validator"
print("acceptance passed")
