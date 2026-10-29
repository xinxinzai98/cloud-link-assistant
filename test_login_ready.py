from pathlib import Path
import unittest
from unittest import mock

import login_ready


class LoginReadyTests(unittest.TestCase):
    def test_waits_through_proxy_and_relay_startup_before_launch(self):
        relay = object()
        with mock.patch.object(login_ready, 'readiness', side_effect=[
                login_ready.NotReady('proxy_port'), login_ready.NotReady('cloud_relay'),
                ('127.0.0.1', 7897)]) as check, \
                mock.patch.object(login_ready.time, 'sleep') as sleep, \
                mock.patch.object(login_ready, 'report'):
            self.assertEqual(login_ready.wait_ready(relay, 30), ('127.0.0.1', 7897))
            self.assertEqual(check.call_count, 3)
            self.assertEqual(sleep.call_count, 2)

    def test_offline_timeout_does_not_open_any_app(self):
        with mock.patch.object(login_ready.time, 'monotonic', side_effect=[0, 1, 601]), \
                mock.patch.object(login_ready.time, 'sleep'), \
                mock.patch.object(login_ready, 'readiness', side_effect=login_ready.NotReady('cloud_relay')), \
                mock.patch.object(login_ready, 'report'), \
                mock.patch.object(login_ready.subprocess, 'run') as run:
            self.assertIsNone(login_ready.wait_ready(object(), 600))
            run.assert_not_called()

    def test_existing_apps_are_not_reopened_or_stopped(self):
        with mock.patch.object(login_ready, 'application_running', return_value=True), \
                mock.patch.object(login_ready.subprocess, 'run') as run, \
                mock.patch.object(login_ready, 'report'):
            login_ready.launch_ready_apps(Path('/helper.app'))
            run.assert_not_called()

    def test_helper_then_existing_proxy_launcher(self):
        helper = Path('/helper.app')
        with mock.patch.object(login_ready, 'application_running', return_value=False), \
                mock.patch.object(login_ready.subprocess, 'run') as run, \
                mock.patch.object(login_ready, 'report'):
            login_ready.launch_ready_apps(helper)
            self.assertEqual(run.call_args_list[0].args[0],
                             ['/usr/bin/open', '-g', '-a', str(helper)])
            self.assertEqual(run.call_args_list[1].args[0][0], '/bin/zsh')
            self.assertTrue(run.call_args_list[1].args[0][1].endswith('通过系统代理启动Codex.command'))

    def test_a_stopped_relay_blocks_readiness_without_restarting_it(self):
        relay = mock.Mock()
        relay.system_proxy.return_value = ('127.0.0.1', 7897)
        relay.control_request.return_value = None
        relay.read_state.return_value = {'phase': 'stopped', 'worker_exit_code': 0}
        with mock.patch.object(login_ready.socket, 'create_connection'):
            with self.assertRaisesRegex(login_ready.NotReady, '^cloud_relay$'):
                login_ready.readiness(relay)
        relay.start.assert_not_called()
        relay.stop.assert_not_called()

    def test_check_mode_never_opens_applications(self):
        with mock.patch('sys.argv', ['login_ready.py', '--helper-app', '/helper.app', '--check']), \
                mock.patch.object(login_ready, 'load_relay'), \
                mock.patch.object(login_ready, 'readiness'), \
                mock.patch.object(login_ready, 'report'), \
                mock.patch.object(login_ready, 'open_if_needed') as open_app, \
                mock.patch.object(login_ready, 'launch_ready_apps') as launch:
            login_ready.main()
            open_app.assert_not_called()
            launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
