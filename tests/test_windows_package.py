import runpy
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class WindowsPackageSourceTests(unittest.TestCase):
    def test_packaging_inputs_are_present(self):
        for relative in (
            "packaging/windows/PilferedParrot.spec",
            "packaging/windows/README-WINDOWS.txt",
            "packaging/windows/build.ps1",
            "packaging/windows/entrypoint.py",
            "PilferedParrot.cmd",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_spec_evaluates_to_onedir_build_with_repo_assets(self):
        calls = {}

        class AnalysisResult:
            pure = ["pure"]
            scripts = ["script"]
            binaries = ["binary"]
            datas = ["data"]

        def analysis(*args, **kwargs):
            calls["analysis"] = (args, kwargs)
            return AnalysisResult()

        def pyz(*args, **kwargs):
            calls["pyz"] = (args, kwargs)
            return "pyz"

        def exe(*args, **kwargs):
            calls["exe"] = (args, kwargs)
            return "exe"

        def collect(*args, **kwargs):
            calls["collect"] = (args, kwargs)
            return "collect"

        hooks = types.ModuleType("PyInstaller.utils.hooks")
        hook_call = {}
        def collect_data_files(package, **kwargs):
            hook_call["package"] = package
            hook_call["kwargs"] = kwargs
            return [("asset", package)]
        hooks.collect_data_files = collect_data_files
        package = types.ModuleType("PyInstaller")
        utils = types.ModuleType("PyInstaller.utils")
        package.utils = utils
        utils.hooks = hooks
        old_modules = {name: sys.modules.get(name) for name in ("PyInstaller", "PyInstaller.utils", "PyInstaller.utils.hooks")}
        sys.modules.update({"PyInstaller": package, "PyInstaller.utils": utils, "PyInstaller.utils.hooks": hooks})
        try:
            runpy.run_path(
                str(ROOT / "packaging/windows/PilferedParrot.spec"),
                init_globals={
                    "Analysis": analysis, "PYZ": pyz, "EXE": exe,
                    "COLLECT": collect, "SPECPATH": str(ROOT / "packaging/windows"),
                },
            )
        finally:
            for name, module in old_modules.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
        analysis_args, analysis_kwargs = calls["analysis"]
        self.assertEqual(analysis_kwargs["pathex"], [str(ROOT)])
        self.assertEqual(hook_call, {"package": "pilferedparrot", "kwargs": {"include_py_files": True}})
        self.assertTrue(any(path == str(ROOT / "config.example.json") for path, _ in analysis_kwargs["datas"]))
        self.assertTrue(any(path == str(ROOT / "packaging/windows/README-WINDOWS.txt") for path, _ in analysis_kwargs["datas"]))
        self.assertTrue(calls["exe"][1]["exclude_binaries"])
        self.assertEqual(calls["collect"][1]["name"], "PilferedParrot")

    def test_versioned_archive_and_executable_names_are_fixed(self):
        build = (ROOT / "packaging/windows/build.ps1").read_text()
        self.assertIn('$Version = "0.6.1"', build)
        self.assertIn("PilferedParrot-$Version-windows-x64.zip", build)
        self.assertIn('PilferedParrot.exe', build)

    def test_source_launcher_passes_arguments_to_windows_module(self):
        launcher = (ROOT / "PilferedParrot.cmd").read_text()
        self.assertRegex(launcher, r"%PYTHON%\s+-m\s+pilferedparrot\.windows\s+%\*")

    def test_sha256_manifest_format(self):
        build = (ROOT / "packaging/windows/build.ps1").read_text()
        self.assertIn("Get-FileHash -Algorithm SHA256", build)
        self.assertIn("SHA256SUMS", build)

if __name__ == "__main__":
    unittest.main()
