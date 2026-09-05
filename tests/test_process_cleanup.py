import os
import signal
import subprocess
import unittest
from unittest.mock import MagicMock, call, patch

from pilferedparrot import dispatch


def _running_process(pid=1234):
    process = MagicMock()
    process.pid = pid
    process.poll.return_value = None
    return process


class ProcessCleanupTests(unittest.TestCase):
    def test_invalid_pid_values_fail_closed_without_any_termination(self):
        values = [MagicMock(), None, 0, 1, -7, True, False, "1234", 9999]
        with patch.object(dispatch.os, "getpid", return_value=9999), \
             patch.object(dispatch.os, "getpgrp", return_value=9998, create=True), \
             patch.object(dispatch.os, "killpg", create=True) as killpg, \
             patch.object(dispatch.subprocess, "run") as run:
            for platform in ("linux", "win32"):
                with patch.object(dispatch.sys, "platform", platform):
                    for value in values:
                        process = _running_process(value)
                        dispatch._stop_process(process)
                        process.terminate.assert_not_called()
                        process.kill.assert_not_called()
                        process.wait.assert_not_called()
        killpg.assert_not_called()
        run.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX process groups")
    def test_current_pid_and_process_group_fail_closed(self):
        with patch.object(dispatch.os, "getpid", return_value=4321), \
             patch.object(dispatch.os, "getpgrp", return_value=5432), \
             patch.object(dispatch.sys, "platform", "linux"), \
             patch.object(dispatch.os, "killpg") as killpg:
            for value in (4321, 5432):
                process = _running_process(value)
                dispatch._stop_process(process)
                process.terminate.assert_not_called()
                process.kill.assert_not_called()
                process.wait.assert_not_called()
        killpg.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX process groups")
    def test_valid_posix_pid_uses_its_process_group(self):
        process = _running_process(2468)
        with patch.object(dispatch.os, "getpid", return_value=9999), \
             patch.object(dispatch.os, "getpgrp", return_value=9998), \
             patch.object(dispatch.sys, "platform", "linux"), \
             patch.object(dispatch.os, "killpg") as killpg:
            dispatch._stop_process(process)
        killpg.assert_called_once_with(2468, signal.SIGTERM)
        process.wait.assert_called_once_with(timeout=2)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX process groups")
    def test_timeout_escalates_to_sigkill(self):
        process = _running_process(2468)
        process.wait.side_effect = [subprocess.TimeoutExpired("provider", 2), None]
        with patch.object(dispatch.os, "getpid", return_value=9999), \
             patch.object(dispatch.os, "getpgrp", return_value=9998), \
             patch.object(dispatch.sys, "platform", "linux"), \
             patch.object(dispatch.os, "killpg") as killpg:
            dispatch._stop_process(process)
        self.assertEqual(killpg.call_args_list, [
            ((2468, signal.SIGTERM),), ((2468, signal.SIGKILL),),
        ])
        process.kill.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX process groups")
    def test_signal_failure_falls_back_to_process_termination(self):
        process = _running_process(2468)
        with patch.object(dispatch.os, "getpid", return_value=9999), \
             patch.object(dispatch.os, "getpgrp", return_value=9998), \
             patch.object(dispatch.sys, "platform", "linux"), \
             patch.object(dispatch.os, "killpg", side_effect=OSError):
            dispatch._stop_process(process)
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=2)

    def test_windows_taskkill_receives_validated_numeric_pid(self):
        process = _running_process(2468)
        with patch.object(dispatch.sys, "platform", "win32"), \
             patch.object(dispatch.os, "getpid", return_value=9999), \
             patch.object(dispatch.subprocess, "run") as run:
            dispatch._stop_process(process)
        run.assert_called_once_with(
            ["taskkill", "/PID", "2468", "/T", "/F"],
            stdout=dispatch.subprocess.DEVNULL, stderr=dispatch.subprocess.DEVNULL,
            timeout=2, check=False,
        )
        self.assertEqual(process.wait.call_args_list, [
            call(timeout=1), call(timeout=2),
        ])
        process.terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
