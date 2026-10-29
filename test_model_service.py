import contextlib
import json
from pathlib import Path
import plistlib
import tempfile
import types
import unittest
from unittest import mock

import autostart
import https_compat
import model_service as service


class ModelServiceTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            home = Path(directory).resolve()
            support = home / 'model'
            agents = home / 'LaunchAgents'
            agents.mkdir()
            config = home / '.codex/config.toml'
            config.parent.mkdir()
            config.write_text('model_provider = "openai"\nmodel = "fixture"\n[tools]\nvalue = "keep-me"\n')
            app = home / 'Helper.app'
            (app / 'Contents').mkdir(parents=True)
            (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'local.hive.cloudlinkassistant'}))
            values = {'HOME': home, 'SUPPORT': support, 'CONFIG': config, 'AGENTS': agents,
                      'AGENT': agents / (service.LABEL + '.plist'),
                      'LOGIN': agents / (service.LOGIN_LABEL + '.plist'),
                      'SYSTEM': home / 'system', 'CLOUD_DAEMON': home / 'cloud-daemon.plist'}
            for name, value in values.items():
                stack.enter_context(mock.patch.object(service, name, value))
            run = stack.enter_context(mock.patch.object(service, 'command', return_value=types.SimpleNamespace(returncode=0)))
            stack.enter_context(mock.patch.object(service, 'loaded', return_value=False))
            stack.enter_context(mock.patch.object(service.socket, 'socket'))
            yield app, values, run

    def test_config_add_remove_preserves_all_unrelated_fields_and_concurrent_changes(self):
        before = b'model_provider="openai"\n# keep\n[tools]\nsecret="synthetic-fixture-only"\n'
        changed = service.config_change(before, True)
        parsed = service.tomllib.loads(changed.decode())
        self.assertEqual(parsed['tools']['secret'], 'synthetic-fixture-only')
        changed += b'new_user_field="preserved"\n'
        restored = service.config_change(changed, False)
        result = service.tomllib.loads(restored.decode())
        self.assertNotIn('openai_base_url', result)
        self.assertEqual(result['tools']['new_user_field'], 'preserved')
        self.assertIn(b'# keep', restored)

    def test_custom_provider_or_endpoint_is_never_overwritten(self):
        for before in [b'model_provider="custom"\n', b'openai_base_url="https://custom.invalid"\n']:
            with self.assertRaises(RuntimeError):
                service.config_change(before, True)
        self.assertEqual(service.config_change(b'openai_base_url="https://custom.invalid"\n', False),
                         b'openai_base_url="https://custom.invalid"\n')

    def test_enable_fresh_install_preserves_model_and_stages_all_resources(self):
        with self.fixture() as (app, v, run), mock.patch.object(service, 'health', return_value={'running': True}):
            # First health read: no existing listener. Later reads: our new service.
            with mock.patch.object(service, 'health', side_effect=[{}, {'running': True}]):
                service.enable(app)
            config = service.tomllib.loads(v['CONFIG'].read_text())
            self.assertEqual(config['openai_base_url'], service.URL)
            self.assertEqual(config['model'], 'fixture')
            self.assertTrue(all((v['SUPPORT'] / f).is_file() for f in service.FILES))
            self.assertTrue(json.loads((v['SUPPORT'] / 'install-state.json').read_text())['enabled'])
            self.assertFalse(v['LOGIN'].exists())

    def test_failed_registration_rolls_back_configuration_and_new_agent(self):
        with self.fixture() as (app, v, run), mock.patch.object(service, 'health', return_value={}):
            before = v['CONFIG'].read_bytes()
            def failing(*args, **kwargs):
                if args[:2] == ('/bin/launchctl', 'bootstrap') and args[-1] == str(v['AGENT']):
                    raise RuntimeError('fixture bootstrap failure')
                return types.SimpleNamespace(returncode=0)
            run.side_effect = failing
            with self.assertRaises(RuntimeError):
                service.enable(app)
            self.assertEqual(v['CONFIG'].read_bytes(), before)
            self.assertFalse(v['AGENT'].exists())
            self.assertFalse((v['SUPPORT'] / 'install-state.json').exists())

    def test_adopts_legacy_running_gateway_without_stopping_it(self):
        with self.fixture() as (app, v, run):
            v['SUPPORT'].mkdir()
            (v['SUPPORT'] / 'https_compat.py').write_text('legacy fixture')
            (v['SUPPORT'] / 'install-state.json').write_text('{"installed":true}')
            v['AGENT'].write_bytes(plistlib.dumps({'Label': service.LABEL, 'ProgramArguments': ['python', str(v['SUPPORT'] / 'https_compat.py'), '--port', '19847']}))
            with mock.patch.object(service, 'loaded', side_effect=lambda label: label == service.LABEL), \
                    mock.patch.object(service, 'health', return_value={'running': True}):
                service.enable(app)
                self.assertTrue(service.status()['pending_code_restart'])
            for call in run.call_args_list:
                self.assertFalse(call.args[:2] == ('/bin/launchctl', 'bootout') and call.args[-1].endswith('/' + service.LABEL))

    def test_restore_only_config_keeps_existing_service_and_other_fields(self):
        with self.fixture() as (app, v, run):
            v['CONFIG'].write_bytes(service.config_change(v['CONFIG'].read_bytes(), True))
            service.restore()
            self.assertNotIn('openai_base_url', service.tomllib.loads(v['CONFIG'].read_text()))
            self.assertEqual(service.tomllib.loads(v['CONFIG'].read_text())['tools']['value'], 'keep-me')
            run.assert_not_called()

    def test_unknown_existing_listener_is_not_adopted(self):
        with self.fixture() as (app, v, run), mock.patch.object(service, 'health', return_value={'running': True}):
            before = v['CONFIG'].read_bytes()
            with self.assertRaisesRegex(RuntimeError, '未接管'):
                service.enable(app)
            run.assert_not_called()
            self.assertEqual(v['CONFIG'].read_bytes(), before)

    def test_ordered_login_updates_only_owned_agent_and_can_restore_helper(self):
        with self.fixture() as (app, v, run):
            v['CLOUD_DAEMON'].touch()
            v['LOGIN'].write_bytes(plistlib.dumps(service.login_job(app, False)))
            self.assertTrue(service.configure_login(app, True))
            self.assertIn(str(v['SUPPORT'] / 'login_ready.py'), plistlib.loads(v['LOGIN'].read_bytes())['ProgramArguments'])
            service.configure_login(app, False)
            self.assertEqual(plistlib.loads(v['LOGIN'].read_bytes())['ProgramArguments'], ['/usr/bin/open', '-a', str(app)])

    def test_does_not_replace_customized_helper_login(self):
        with self.fixture() as (app, v, run):
            v['CLOUD_DAEMON'].touch()
            before = plistlib.dumps({'Label': service.LOGIN_LABEL, 'ProgramArguments': ['custom-command']})
            v['LOGIN'].write_bytes(before)
            with self.assertRaisesRegex(RuntimeError, '未覆盖'):
                service.configure_login(app, True)
            self.assertEqual(v['LOGIN'].read_bytes(), before)
            run.assert_not_called()

    def test_enable_refuses_custom_login_before_starting_or_writing(self):
        with self.fixture() as (app, v, run):
            v['CLOUD_DAEMON'].touch()
            v['LOGIN'].write_bytes(plistlib.dumps({'Label': service.LOGIN_LABEL, 'ProgramArguments': ['custom-command']}))
            before = v['CONFIG'].read_bytes()
            with self.assertRaisesRegex(RuntimeError, '未覆盖'):
                service.enable(app)
            self.assertEqual(v['CONFIG'].read_bytes(), before)
            self.assertFalse(v['SUPPORT'].exists())
            run.assert_not_called()

    def test_concurrent_config_edit_is_kept_when_install_rolls_back(self):
        with self.fixture() as (app, v, run):
            before = v['CONFIG'].read_bytes()
            concurrent = before + b'user_later_field="preserved"\n'
            def edit_during_start(*args, **kwargs):
                if args[:2] == ('/bin/launchctl', 'kickstart'):
                    v['CONFIG'].write_bytes(concurrent)
                return types.SimpleNamespace(returncode=0)
            run.side_effect = edit_during_start
            with mock.patch.object(service, 'health', side_effect=[{}, {'running': True}]):
                with self.assertRaises(RuntimeError):
                    service.enable(app)
            self.assertEqual(v['CONFIG'].read_bytes(), concurrent)
            self.assertFalse(v['AGENT'].exists())

    def test_explicit_stop_restores_config_login_and_disables_only_model_agent(self):
        with self.fixture() as (app, v, run), mock.patch.object(service, 'health', return_value={}):
            v['CLOUD_DAEMON'].touch()
            v['SUPPORT'].mkdir()
            (v['SUPPORT'] / 'install-state.json').write_text(json.dumps({'format': 2, 'enabled': True, 'app': str(app)}))
            v['CONFIG'].write_bytes(service.config_change(v['CONFIG'].read_bytes(), True))
            v['AGENT'].write_bytes(plistlib.dumps({'Label': service.LABEL, 'ProgramArguments': ['python', str(v['SUPPORT'] / 'https_compat.py')]}))
            v['LOGIN'].write_bytes(plistlib.dumps(service.login_job(app, True)))
            service.restore(True, app)
            self.assertNotIn('openai_base_url', service.tomllib.loads(v['CONFIG'].read_text()))
            self.assertFalse(v['AGENT'].exists())
            self.assertEqual(len(list(v['AGENTS'].glob(service.LABEL + '.plist.disabled-*'))), 1)
            self.assertEqual(plistlib.loads(v['LOGIN'].read_bytes())['ProgramArguments'], ['/usr/bin/open', '-a', str(app)])
            self.assertTrue(v['CLOUD_DAEMON'].exists())
            self.assertFalse(any('system/' in str(call.args) for call in run.call_args_list))

    def test_cloud_autostart_reinstall_keeps_ready_gate_when_model_enabled(self):
        with self.fixture() as (app, v, run):
            model = v['HOME'] / 'Library/Application Support/云连接助手/model-https-compat'
            model.mkdir(parents=True)
            (model / 'login_ready.py').touch()
            (model / 'install-state.json').write_text('{"enabled":true}')
            daemon, login = autostart.jobs({'home': str(v['HOME']), 'uid': 501, 'gid': 20}, app)
            self.assertIn(str(model / 'login_ready.py'), login['ProgramArguments'])
            self.assertEqual(daemon['KeepAlive'], {'SuccessfulExit': False})

    def test_gateway_reads_current_loopback_system_proxy_with_verified_tls(self):
        proxy_reader = types.SimpleNamespace(system_proxy=lambda: ('127.0.0.1', 8123))
        spec = mock.Mock()
        with mock.patch.object(https_compat.importlib.util, 'spec_from_file_location', return_value=spec), \
                mock.patch.object(https_compat.importlib.util, 'module_from_spec', return_value=proxy_reader), \
                mock.patch.object(https_compat.http.client, 'HTTPSConnection') as connect:
            https_compat.official_connection()
            self.assertEqual(connect.call_args.args, ('127.0.0.1', 8123))
            context = connect.call_args.kwargs['context']
            self.assertTrue(context.check_hostname)
            connect.return_value.set_tunnel.assert_called_once_with('chatgpt.com', 443)


if __name__ == '__main__':
    unittest.main()
