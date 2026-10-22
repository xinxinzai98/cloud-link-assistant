import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('anti', Path(__file__).with_name('antigravity_helper.py'))
anti = importlib.util.module_from_spec(spec)
spec.loader.exec_module(anti)


class AntigravityTests(unittest.TestCase):
    def test_running_app_is_only_opened_without_force_quit(self):
        with mock.patch.object(anti.APP.__class__, 'exists', return_value=True), mock.patch.object(anti.subprocess, 'run') as run:
            run.side_effect = [subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0)]
            anti.launch()
            self.assertEqual(run.call_args_list[1].args[0], ['/usr/bin/open', '-a', str(anti.APP)])
            self.assertEqual(run.call_count, 2)

    def test_proxy_launch_keeps_authentication_inside_official_app(self):
        with mock.patch.object(anti.APP.__class__, 'exists', return_value=True), mock.patch.object(anti.subprocess, 'run') as run, mock.patch.object(anti, 'proxy_settings', return_value=('127.0.0.1', 7897)):
            run.side_effect = [subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0)]
            anti.launch()
            command = run.call_args_list[1].args[0]
            self.assertIn('HTTPS_PROXY=http://127.0.0.1:7897', command)
            self.assertIn('--proxy-server=http://127.0.0.1:7897', command)
            self.assertNotIn('-n', command)


if __name__ == '__main__':
    unittest.main()
