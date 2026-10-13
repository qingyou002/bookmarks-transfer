import threading
import ctypes
import os
import unittest
from unittest.mock import patch

from browser_transfer.models import MigrationError, Profile
from browser_transfer.windows import close_browser_processes
from browser_transfer.windows import browser_processes, _terminate_browser
from browser_transfer.engine import Engine
from pathlib import Path


class CloseTests(unittest.TestCase):
    def test_already_closed_and_normal_exit_never_terminate(self):
        with patch("browser_transfer.windows.browser_processes", return_value={"Edge": [], "Chrome": []}), \
             patch("browser_transfer.windows._request_window_close") as close, \
             patch("browser_transfer.windows._terminate_browser") as terminate:
            self.assertEqual(close_browser_processes(), {"Edge": 0, "Chrome": 0})
            close.assert_called_once_with(set())
            terminate.assert_not_called()

    def test_normal_exit_wait_and_background_remain(self):
        with patch("browser_transfer.windows.browser_processes", side_effect=[{"Edge": [1]}, {"Edge": []}]), \
             patch("browser_transfer.windows._request_window_close") as close, \
             patch("browser_transfer.windows._terminate_browser") as terminate:
            self.assertEqual(close_browser_processes(["Edge"]), {"Edge": 0})
            close.assert_called_once_with({1})
            terminate.assert_not_called()
        with patch("browser_transfer.windows.browser_processes", return_value={"Edge": [1]}), \
             patch("browser_transfer.windows._request_window_close"), \
             patch("browser_transfer.windows._terminate_browser") as terminate:
            self.assertEqual(close_browser_processes(["Edge"], timeout=0), {"Edge": 1})
            terminate.assert_not_called()

    def test_force_is_explicit_and_failure_not_hidden(self):
        with patch("browser_transfer.windows.browser_processes", side_effect=[{"Chrome": [2]}, {"Chrome": []}]), \
             patch("browser_transfer.windows._terminate_browser") as terminate:
            self.assertEqual(close_browser_processes(["Chrome"], force=True), {"Chrome": 0})
            terminate.assert_called_once_with(2, "Chrome")
        with patch("browser_transfer.windows.browser_processes", return_value={"Edge": [1]}), \
             patch("browser_transfer.windows._terminate_browser", side_effect=MigrationError("无法结束")):
            with self.assertRaises(MigrationError):
                close_browser_processes(["Edge"], force=True)

    def test_check_failure_does_not_close_any_process(self):
        with patch("browser_transfer.windows.browser_processes", side_effect=MigrationError("无法检查")), \
             patch("browser_transfer.windows._request_window_close") as close:
            with self.assertRaises(MigrationError):
                close_browser_processes()
            close.assert_not_called()

    def test_cancel_wait_returns_without_force(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch("browser_transfer.windows.browser_processes", return_value={"Edge": [1]}), \
             patch("browser_transfer.windows._request_window_close"), \
             patch("browser_transfer.windows._terminate_browser") as terminate:
            self.assertEqual(close_browser_processes(["Edge"], cancelled=cancelled), {"Edge": 1})
            terminate.assert_not_called()

    def test_engine_requires_normal_click_before_force_and_rechecks_restarts(self):
        requests = []
        answers = iter(["close_force", "close_normal", "continue"])
        def prompt(request):
            requests.append(request)
            return next(answers)
        profile = Profile("Edge", Path("unused"), "Default")
        with patch("browser_transfer.engine.assert_closed", side_effect=[MigrationError("仍运行"), MigrationError("又启动"), None]), \
             patch("browser_transfer.engine.browser_processes", return_value={"Edge": [1]}), \
             patch("browser_transfer.engine.close_browser_processes", return_value={"Edge": 0}) as close:
            Engine(prompt=prompt).close_browsers([profile])
            close.assert_called_once()
            self.assertFalse(close.call_args.kwargs["force"])
            self.assertFalse(requests[0]["can_force"])

    def test_engine_force_offered_only_after_normal_close_and_can_be_refused(self):
        requests = []
        answers = iter(["close_normal", "continue"])
        def prompt(request):
            requests.append(request)
            return next(answers)
        profile = Profile("Chrome", Path("unused"), "Default")
        with patch("browser_transfer.engine.assert_closed", side_effect=[MigrationError("后台残留"), None]), \
             patch("browser_transfer.engine.browser_processes", return_value={"Chrome": [2]}), \
             patch("browser_transfer.engine.close_browser_processes", return_value={"Chrome": 1}) as close:
            Engine(prompt=prompt).close_browsers([profile])
            self.assertTrue(requests[1]["can_force"])
            self.assertEqual(close.call_count, 1)

    def test_toolbar_click_starts_normal_close_directly(self):
        profile = Profile("Chrome", Path("unused"), "Default")
        prompt = unittest.mock.Mock()
        with patch("browser_transfer.engine.assert_closed"), \
             patch("browser_transfer.engine.close_browser_processes", return_value={"Chrome": 0}) as close:
            Engine(prompt=prompt).close_browsers([profile], initial_action="close_normal")
            close.assert_called_once()
            self.assertFalse(close.call_args.kwargs["force"])
            prompt.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Windows native adapter")
class NativeCloseTests(unittest.TestCase):
    def test_enumeration_filters_current_session_and_exact_images(self):
        rows = iter([(10, "msedge.exe", 7), (11, "chrome.exe", 7),
                     (12, "msedgewebview2.exe", 7), (13, "msedge.exe", 8)])
        sessions = {1: 7}
        kernel = unittest.mock.Mock()
        kernel.GetCurrentProcessId.return_value = 1
        kernel.CreateToolhelp32Snapshot.return_value = 123
        def read(_, pointer):
            row = next(rows, None)
            if row is None:
                ctypes.set_last_error(18)
                return False
            pid, image, session = row
            pointer._obj.th32ProcessID, pointer._obj.szExeFile = pid, image
            sessions[pid] = session
            return True
        def session(pid, pointer):
            pointer._obj.value = sessions[pid]
            return True
        kernel.Process32FirstW.side_effect = kernel.Process32NextW.side_effect = read
        kernel.ProcessIdToSessionId.side_effect = session
        with patch("browser_transfer.windows._kernel", return_value=kernel):
            self.assertEqual(browser_processes(), {"Edge": [10], "Chrome": [11]})
        kernel.CloseHandle.assert_called_once_with(123)

    def test_force_rejects_reused_image_and_other_sessions(self):
        for image, session in (("msedgewebview2.exe", 7), ("msedge.exe", 8)):
            kernel = unittest.mock.Mock()
            kernel.OpenProcess.return_value = 123
            kernel.GetCurrentProcessId.return_value = 1
            def query(_, __, buffer, size):
                buffer.value = "C:\\test\\" + image
                return True
            def get_session(pid, pointer):
                pointer._obj.value = 7 if pid == 1 else session
                return True
            kernel.QueryFullProcessImageNameW.side_effect = query
            kernel.ProcessIdToSessionId.side_effect = get_session
            with patch("browser_transfer.windows._kernel", return_value=kernel):
                with self.assertRaises(MigrationError):
                    _terminate_browser(10, "Edge")
            kernel.TerminateProcess.assert_not_called()
            kernel.CloseHandle.assert_called_once_with(123)
