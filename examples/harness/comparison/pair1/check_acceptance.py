"""Independent contract: newline text -> trimmed, nonempty, stable distinct labels."""
import importlib.util
from pathlib import Path
import sys
arm = sys.argv[1]
assert arm in {"direct", "delegated"}
p = Path(__file__).parent / arm / "labels.py"
spec = importlib.util.spec_from_file_location("labels", p)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
for value, expected in [("", []), (" A \n\nB\r\nA\n b ", ["A", "B", "b"]), ("é\né\nZ", ["é", "Z"]), (" \t\n", [])]:
    assert m.normalize(value) == expected, (value, expected)
print("acceptance passed")
