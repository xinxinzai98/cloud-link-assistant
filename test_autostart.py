import threading
import contextlib
import json
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import autostart


class AutostartTests(unittest.TestCase):
    def bypass_fixture(self, added=True, phase='running'):
        relay = mock.Mock()
        relay.TARGET = 'codex-cloud-backend.chatgpt.com'
        relay.read_state.return_value = {'phase': phase, 'proxy_bypass': {'service': 'Wi-Fi', 'added': added}}
        relay.read_bypass.return_value = ['localhost', 'new-user-exception.example']
        relay.control_request.return_value = {'running': True}
        return relay

    def test_repairs_overwritten_exception_preserving_current_user_entries(self):
        relay = self.bypass_fixture()
        self.assertTrue(autostart.repair_bypass(relay))
        relay.set_bypass.assert_called_once_with('Wi-Fi', ['localhost', 'new-user-exception.example', relay.TARGET])

    def test_does_not_duplicate_present_exception(self):
        relay = self.bypass_fixture()
        relay.read_bypass.return_value.append(relay.TARGET.upper())
        self.assertFalse(autostart.repair_bypass(relay))
        relay.set_bypass.assert_not_called()

    def test_leaves_unowned_exception_and_stopped_relay_alone(self):
        for relay in [self.bypass_fixture(added=False), self.bypass_fixture(phase='stopped')]:
            self.assertFalse(autostart.repair_bypass(relay))
            relay.read_bypass.assert_not_called()

    def test_manual_stop_during_repair_restores_owned_exception(self):
        relay = self.bypass_fixture()
        relay.control_request.return_value = None
        self.assertFalse(autostart.repair_bypass(relay))
        relay.restore_bypass.assert_called_once_with({'service': 'Wi-Fi', 'added': True})

    def test_monitor_continues_after_bypass_read_failure(self):
        relay = self.bypass_fixture()
        relay.control_request.side_effect = [{'running': True}, {'running': True}, None]
        relay.read_bypass.side_effect = RuntimeError('settings temporarily unavailable')
        relay.read_state.side_effect = [
            {'phase': 'running', 'proxy_bypass': {'service': 'Wi-Fi', 'added': True}},
            {'phase': 'stopped', 'worker_exit_code': 0},
        ]
        stopping = threading.Event()
        with mock.patch.object(stopping, 'wait', return_value=False):
            self.assertEqual(autostart.supervise(relay, 501, stopping), 0)

    @contextlib.contextmanager
    def installed_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            support = root / 'protected'
            state = support / 'relay-state'
            state.mkdir(parents=True)
            (state / 'hosts-before-test.bin').write_bytes(b'original recovery data')
            user = {'uid': os.getuid(), 'gid': os.getgid(), 'home': str(root / 'home')}
            original = autostart.old_state(user)
            original.parent.mkdir(parents=True)
            original.symlink_to(state, target_is_directory=True)
            autostart.agent_path(user).parent.mkdir(parents=True)
            daemon = root / 'LaunchDaemons' / 'relay.plist'
            daemon.parent.mkdir()
            app = root / 'App.app'
            resources = app / 'Contents/Resources'
            resources.mkdir(parents=True)
            for path in [support / 'relay.py', resources / 'relay.py']:
                path.write_bytes(b'fixed relay fixture')
            (support / 'autostart.py').write_bytes(b'previous protected installer')
            (support / 'operator.json').write_text(json.dumps(user))
            (support / 'installation.json').write_text(json.dumps({'installed_at': 1}))
            relay = mock.Mock()
            relay.control_request.return_value = None
            relay.atomic_write.side_effect = lambda path, data: path.write_bytes(data)
            with mock.patch.multiple(autostart, SUPPORT=support, DAEMON_PLIST=daemon,
                                     trusted=mock.Mock(), require_root=mock.Mock(),
                                     load_relay=mock.Mock(return_value=relay)), \
                    mock.patch.object(autostart.os, 'chown'), \
                    mock.patch.object(autostart.subprocess, 'run'):
                yield support, user, app, resources, daemon, relay

    def test_can_disable_and_enable_repeatedly_without_losing_recovery_data(self):
        with self.installed_fixture() as (support, user, app, resources, daemon, relay):
            for _ in range(2):
                autostart.reactivate(user, app, resources)
                self.assertTrue(daemon.exists())
                self.assertTrue(autostart.agent_path(user).exists())
                autostart.disable()
                self.assertFalse(daemon.exists())
                self.assertFalse(autostart.agent_path(user).exists())
            self.assertEqual(len(list(support.glob('*.disabled-*'))), 4)
            self.assertEqual((support / 'relay-state/hosts-before-test.bin').read_bytes(), b'original recovery data')
            self.assertTrue(autostart.old_state(user).is_symlink())

    def test_reactivation_rejects_other_user(self):
        with self.installed_fixture() as (support, user, app, resources, daemon, relay):
            (support / 'operator.json').write_text(json.dumps(dict(user, uid=user['uid'] + 1)))
            with self.assertRaisesRegex(RuntimeError, '其他用户'):
                autostart.reactivate(user, app, resources)
            relay.atomic_write.assert_not_called()

    def test_reactivation_does_not_overwrite_existing_jobs(self):
        with self.installed_fixture() as (support, user, app, resources, daemon, relay):
            daemon.write_bytes(b'existing job')
            with self.assertRaisesRegex(RuntimeError, '已开启'):
                autostart.reactivate(user, app, resources)
            self.assertEqual(daemon.read_bytes(), b'existing job')
            relay.atomic_write.assert_not_called()

    def test_reactivation_rolls_back_failed_registration(self):
        with self.installed_fixture() as (support, user, app, resources, daemon, relay):
            def register(args, **kwargs):
                if args[:3] == ['/bin/launchctl', 'bootstrap', 'gui/' + str(user['uid'])]:
                    raise subprocess.CalledProcessError(1, args)
            with mock.patch.object(autostart.subprocess, 'run', side_effect=register):
                with self.assertRaises(subprocess.CalledProcessError):
                    autostart.reactivate(user, app, resources)
            self.assertFalse(daemon.exists())
            self.assertFalse(autostart.agent_path(user).exists())
            self.assertEqual((support / 'autostart.py').read_bytes(), b'previous protected installer')
            self.assertEqual((support / 'relay-state/hosts-before-test.bin').read_bytes(), b'original recovery data')

    def test_state_migration_preserves_backups_and_gui_path(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / 'original'
            original.mkdir()
            (original / 'hosts-backup').write_bytes(b'original hosts')
            inode = (original / 'hosts-backup').stat().st_ino
            destination = Path(directory) / 'protected'
            autostart.migrate_state(original, destination)
            self.assertTrue(original.is_symlink())
            self.assertEqual((original / 'hosts-backup').read_bytes(), b'original hosts')
            self.assertEqual((destination / 'hosts-backup').stat().st_ino, inode)

    def test_state_migration_rolls_back_failed_link(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / 'original'
            original.mkdir()
            destination = Path(directory) / 'protected'
            with mock.patch.object(Path, 'symlink_to', side_effect=OSError('link failed')):
                with self.assertRaises(OSError):
                    autostart.migrate_state(original, destination)
            self.assertTrue(original.is_dir())
            self.assertFalse(original.is_symlink())
            self.assertFalse(destination.exists())

    def test_native_jobs_and_manual_stop_policy(self):
        daemon, agent = autostart.jobs({'uid': 501, 'gid': 20}, '/Applications/助手.app')
        self.assertEqual(daemon['KeepAlive'], {'SuccessfulExit': False})
        self.assertTrue(daemon['RunAtLoad'])
        self.assertIn('-I', daemon['ProgramArguments'])
        self.assertTrue(daemon['ProgramArguments'][0].startswith('/Library/Application Support/'))
        self.assertEqual(agent['ProgramArguments'], ['/usr/bin/open', '-a', '/Applications/助手.app'])
        self.assertNotIn('StartInterval', daemon)

    def test_waits_for_login_without_starting(self):
        relay = mock.Mock()
        relay.control_request.return_value = None
        stopping = threading.Event()
        with mock.patch.object(autostart, 'console_uid', return_value=0), \
                mock.patch.object(stopping, 'wait', side_effect=lambda _: stopping.set()):
            self.assertEqual(autostart.supervise(relay, 501, stopping), 0)
        relay.start.assert_not_called()

    def test_waits_for_proxy_then_manual_stop_does_not_restart(self):
        relay = mock.Mock()
        relay.control_request.return_value = None
        relay.start.side_effect = [RuntimeError('proxy not ready'), None]
        relay.read_state.return_value = {'phase': 'stopped', 'worker_exit_code': 0}
        stopping = threading.Event()
        with mock.patch.object(autostart, 'console_uid', return_value=501), \
                mock.patch.object(stopping, 'wait', return_value=False):
            self.assertEqual(autostart.supervise(relay, 501, stopping), 0)
        self.assertEqual(relay.start.call_count, 2)

    def test_worker_crash_requests_launchd_retry(self):
        relay = mock.Mock()
        relay.control_request.side_effect = [{'running': True}, None]
        relay.read_state.return_value = {'phase': 'stopped', 'worker_exit_code': 1}
        stopping = threading.Event()
        with mock.patch.object(stopping, 'wait', return_value=False):
            self.assertEqual(autostart.supervise(relay, 501, stopping), 1)
        relay.start.assert_not_called()

    def test_shutdown_stops_and_restores_relay(self):
        relay = mock.Mock()
        relay.control_request.return_value = {'running': True}
        stopping = threading.Event()
        with mock.patch.object(stopping, 'wait', return_value=True):
            self.assertEqual(autostart.supervise(relay, 501, stopping), 0)
        relay.stop.assert_called_once()


if __name__ == '__main__':
    unittest.main()
