"""The submitted regression must fail the seeded bug and pass fixed behavior."""
from pathlib import Path
import subprocess
import sys
arm = sys.argv[1]
assert arm in {"direct", "delegated"}
root = Path(__file__).parent / arm
runner = """
import sys, unittest, labels
if sys.argv[1] == 'fixed':
    def fixed(value):
        return None if value is None else int(value)
    labels.parse_count = fixed
import test_labels
suite = unittest.defaultTestLoader.loadTestsFromModule(test_labels)
result = unittest.TextTestRunner().run(suite)
sys.exit(0 if result.wasSuccessful() and result.testsRun > 0 else 1)
"""
fixed = subprocess.run([sys.executable, "-c", runner, "fixed"], cwd=root)
buggy = subprocess.run([sys.executable, "-c", runner, "buggy"], cwd=root)
assert fixed.returncode == 0, "regression must pass fixed behavior and run at least one test"
assert buggy.returncode != 0, "regression must detect the original zero/None defect"
print("acceptance passed: defect detected, fixed behavior passes")
