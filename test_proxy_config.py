from pathlib import Path
import ssl
import subprocess
import tempfile
import unittest
from unittest import mock

import proxy_config as config
import windows_bridge


class ProxySettingsTests(unittest.TestCase):
    def test_manual_settings_round_trip_and_auto_mode_uses_the_system(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(config, 'settings_path', return_value=Path(directory) / 'settings.json'), \
                mock.patch.object(config, 'system_proxy', return_value=('127.0.0.1', 10809)) as system:
            config.save_settings('http://[::1]:8123')
            self.assertEqual(config.resolve(), ('::1', 8123))
            system.assert_not_called()
            config.save_settings('auto')
            self.assertEqual(config.resolve(), ('127.0.0.1', 10809))
            self.assertEqual(config.read_settings(), {'mode': 'auto'})

    def test_invalid_manual_addresses_are_not_saved(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(config, 'settings_path', return_value=Path(directory) / 'settings.json'):
            config.save_settings('127.0.0.1:8123')
            for address in ('socks5://127.0.0.1:1080', 'http://fixture:fixture@127.0.0.1:7890',
                            '192.168.1.10:7890', 'proxy.invalid:7890', '127.0.0.1:0',
                            '127.0.0.1:65536', '127.0.0.1:7890/path', '127.0.0.1'):
                with self.subTest(address=address), self.assertRaises(ValueError):
                    config.save_settings(address)
                self.assertEqual(config.resolve(), ('127.0.0.1', 8123))

    def test_malformed_settings_require_correction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            with mock.patch.object(config, 'settings_path', return_value=path):
                for value in ('{', '[]', '{"mode":"manual"}', '{"mode":"unknown"}'):
                    path.write_text(value)
                    with self.subTest(value=value), self.assertRaises(ValueError):
                        config.resolve()
                config.save_settings('auto')
                self.assertEqual(config.read_settings(), {'mode': 'auto'})

    def test_mac_auto_reads_current_https_or_http_port(self):
        https = 'HTTPSEnable : 1\nHTTPSProxy : 127.0.0.1\nHTTPSPort : 7897\n'
        http = 'HTTPEnable : 1\nHTTPProxy : localhost\nHTTPPort : 10809\n'
        with mock.patch.object(config.sys, 'platform', 'darwin'), \
                mock.patch.object(config.subprocess, 'check_output', side_effect=[https, http, '']):
            self.assertEqual(config.system_proxy(), ('127.0.0.1', 7897))
            self.assertEqual(config.system_proxy(), ('localhost', 10809))
            with self.assertRaisesRegex(RuntimeError, '手动填写'):
                config.system_proxy()

    def test_windows_auto_uses_native_registry_proxy_reader(self):
        with mock.patch.object(config.sys, 'platform', 'win32'), \
                mock.patch.object(config.urllib.request, 'getproxies_registry', create=True,
                                  return_value={'https': 'http://127.0.0.1:10809'}):
            self.assertEqual(config.system_proxy(), ('127.0.0.1', 10809))

    def test_network_service_tracks_default_interface_and_skips_disabled_services(self):
        order = ('(1) Wi-Fi\n(Hardware Port: Wi-Fi, Device: en0)\n'
                 '(2) USB Ethernet\n(Hardware Port: USB, Device: en7)\n'
                 '(*) Disabled\n(Hardware Port: USB, Device: en7)\n')
        with mock.patch.object(config.subprocess, 'check_output',
                               side_effect=[' interface: en7\n', order]):
            self.assertEqual(config.active_network_service(), 'USB Ethernet')

    def test_ipv6_default_route_is_supported(self):
        error = subprocess.CalledProcessError(1, 'route')
        with mock.patch.object(config.subprocess, 'check_output', side_effect=[
                error, ' interface: en3\n', '(1) Ethernet\n(Hardware Port: Ethernet, Device: en3)\n']):
            self.assertEqual(config.active_network_service(), 'Ethernet')

    def test_diagnostics_keep_model_and_cloud_results_separate(self):
        with mock.patch.object(config, 'resolve', return_value=('127.0.0.1', 8123)), \
                mock.patch.object(config.socket, 'create_connection'), \
                mock.patch.object(config, 'check_target', return_value=(True, 'HTTP 401')), \
                mock.patch.object(config, 'check_cloud', return_value=(False, 'HTTP 403')):
            result = config.diagnostics()
        self.assertEqual([ok for _, ok, _ in result], [True, True, False])
        self.assertEqual(result[2], ('云端服务', False, 'HTTP 403'))

    def test_cloud_check_uses_running_relay_with_original_tls_hostname(self):
        raw = mock.MagicMock()
        context = mock.MagicMock()
        response = mock.Mock(status=401)
        with mock.patch.object(config.socket, 'create_connection', return_value=raw), \
                mock.patch.object(config.ssl, 'create_default_context', return_value=context), \
                mock.patch.object(config.http.client, 'HTTPResponse', return_value=response), \
                mock.patch.object(config, 'check_target') as proxy_check:
            ok, detail = config.check_cloud(('127.0.0.1', 8123))
            self.assertTrue(ok)
            self.assertIn('当前云中继', detail)
            context.wrap_socket.assert_called_once_with(raw, server_hostname='codex-cloud-backend.chatgpt.com')
            proxy_check.assert_not_called()
            response.close.assert_called_once()

    def test_cloud_check_without_listener_explicitly_reports_proxy_only_path(self):
        with mock.patch.object(config.socket, 'create_connection', side_effect=ConnectionRefusedError), \
                mock.patch.object(config, 'check_target', return_value=(True, 'HTTP 401')):
            ok, detail = config.check_cloud(('127.0.0.1', 8123))
            self.assertTrue(ok)
            self.assertIn('经所选代理', detail)
            self.assertIn('尚未启动', detail)

    def test_target_401_is_reachable_and_403_is_reported(self):
        conn = mock.MagicMock()
        with mock.patch.object(config.http.client, 'HTTPSConnection', return_value=conn) as connect:
            conn.getresponse.return_value.status = 401
            self.assertTrue(config.check_target(('127.0.0.1', 8123), 'chatgpt.com', '/')[0])
            conn.getresponse.return_value.status = 403
            ok, text = config.check_target(('127.0.0.1', 8123), 'chatgpt.com', '/')
            self.assertFalse(ok)
            self.assertIn('403', text)
            self.assertTrue(connect.call_args.kwargs['context'].check_hostname)
            self.assertEqual(connect.call_args.kwargs['context'].verify_mode, ssl.CERT_REQUIRED)
            conn.set_tunnel.assert_called_with('chatgpt.com', 443)
            self.assertEqual(conn.close.call_count, 2)

    def test_unavailable_proxy_and_tls_failures_have_actionable_results(self):
        with mock.patch.object(config, 'resolve', return_value=('127.0.0.1', 8123)), \
                mock.patch.object(config.socket, 'create_connection', side_effect=ConnectionRefusedError), \
                mock.patch.object(config, 'check_target') as target:
            result = config.diagnostics()
            self.assertIn('未监听', result[0][2])
            target.assert_not_called()
        self.assertIn('证书', config.error_message(ssl.SSLCertVerificationError()))
        self.assertIn('407', config.error_message(OSError('Tunnel connection failed: 407 Proxy Authentication Required')))

    def test_windows_model_reads_saved_proxy_for_each_new_request(self):
        with mock.patch.object(windows_bridge.https_compat, 'Gateway') as gateway, \
                mock.patch.object(windows_bridge.proxy_config, 'resolve',
                                  side_effect=[('127.0.0.1', 8123), ('127.0.0.1', 10809)]), \
                mock.patch.object(windows_bridge.http.client, 'HTTPSConnection') as connect:
            windows_bridge.serve_model(19847)
            factory = gateway.call_args.kwargs['connection_factory']
            factory()
            factory()
            self.assertEqual([call.args for call in connect.call_args_list],
                             [('127.0.0.1', 8123), ('127.0.0.1', 10809)])

    def test_windows_configuration_mode_needs_no_listening_port(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(config, 'settings_path', return_value=Path(directory) / 'settings.json'), \
                mock.patch.object(config, 'system_proxy', side_effect=RuntimeError('manual required')), \
                mock.patch('builtins.input', return_value='127.0.0.1:10809'), \
                mock.patch('sys.argv', ['windows_bridge.py', 'configure']):
            windows_bridge.main()
            self.assertEqual(config.resolve(), ('127.0.0.1', 10809))


if __name__ == '__main__':
    unittest.main()
