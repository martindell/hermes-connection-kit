"""Offline lifecycle/registration integration; all actors and runs are fictional."""
import contextlib
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import test_on_demand
import watchdog as w

m = test_on_demand.m
RUN = 'run_' + 'a' * 32
ROUTE = ('example/private-repository', 42)


class LifecycleIntegration(unittest.TestCase):
    free_port = staticmethod(test_on_demand.Lifecycle.free_port)

    def setUp(self):
        test_on_demand.Lifecycle.setUp(self)
        self.calls = []
        original = self.manager.call

        def manager(*args):
            self.calls.append(args)
            if args[:1] == ('is-active',):
                return 'active'
            if args[:1] == ('show',) and args[1] != m.UNIT:
                path = m.UNIT_DIR / args[1]
                return f'FragmentPath={path}\nUnitFileState=static'
            if len(args) > 1 and args[1] != m.UNIT:
                return ''
            return original(*args)

        self.patchers = [mock.patch.object(m, 'systemctl', manager),
                         mock.patch.object(m, 'observer_installed', return_value=True),
                         mock.patch.object(m, 'observer_config', return_value={'control_port': 18765}),
                         mock.patch.object(m, 'control_ready', return_value=True),
                         mock.patch.object(m, 'observer_action', side_effect=lambda action: self.calls.append(('observer', action)))]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_ready_before_enable_and_timer_connect_disconnect_order(self):
        with mock.patch.object(m, 'ready', side_effect=lambda pid: (self.calls.append(('ready', pid)) or True)):
            m.connect()
        self.assertLess(self.calls.index(('ready', 424242)), self.calls.index(('observer', 'enable')))
        self.assertLess(self.calls.index(('start', m.observer_names()[2])), self.calls.index(('observer', 'enable')))
        self.assertLess(self.calls.index(('observer', 'enable')), self.calls.index(('start', m.observer_names()[1])))
        m.disconnect()
        self.assertLess(self.calls.index(('stop', m.observer_names()[0])), self.calls.index(('observer', 'pause')))
        self.assertLess(self.calls.index(('observer', 'pause')), self.calls.index(('stop', m.UNIT)))

    def test_failed_new_connect_rolls_back_owned_tunnel(self):
        with mock.patch.object(m, 'observer_start', side_effect=m.Refusal('observer failed')):
            with self.assertRaises(m.Refusal):
                m.connect()
        self.assertEqual(self.manager.state, 'inactive')
        self.assertIn(('stop', m.UNIT), self.calls)

    def test_failed_observer_on_existing_ready_tunnel_keeps_tunnel(self):
        m.connect()
        self.calls.clear()
        with mock.patch.object(m, 'observer_start', side_effect=m.Refusal('observer failed')):
            with self.assertRaises(m.Refusal):
                m.connect()
        self.assertEqual(self.manager.state, 'active')
        self.assertNotIn(('stop', m.UNIT), self.calls)

    def test_not_ready_never_enables_observer(self):
        with mock.patch.object(m, 'ready', return_value=False), mock.patch.object(m, 'TIMEOUT', 0):
            with self.assertRaises(m.Refusal):
                m.connect()
        self.assertNotIn(('observer', 'enable'), self.calls)

    def test_control_failure_stops_only_observer_units(self):
        with mock.patch.object(m, 'control_ready', return_value=False), mock.patch.object(m, 'time') as clock:
            clock.monotonic.side_effect = [0, 9]
            with self.assertRaisesRegex(m.Refusal, 'control not ready'):
                m.observer_start()
        self.assertIn(('stop', m.observer_names()[1]), self.calls)
        self.assertNotIn(('stop', m.UNIT), self.calls)

    def test_units_static_bounded_and_credential_not_in_argv(self):
        units = m.observer_units(18765)
        self.assertIn('TimeoutStartSec=44', units[0])
        self.assertIn('OnUnitActiveSec=60s', units[1])
        self.assertIn('LoadCredential=gateway:', units[2])
        self.assertIn('%d/gateway', units[2])
        for unit in units:
            self.assertNotIn('WantedBy=', unit)
            self.assertNotIn('fictional-gateway', unit)
        self.assertIn(str(m.OBSERVER_CONFIG), units[0])
        self.assertNotIn('watchdog.py', m.UNIT_PATH.read_text())

    def test_shadowed_observer_refuses_before_start_or_stop(self):
        with mock.patch.object(m, 'systemctl', return_value='FragmentPath=/unmanaged\nUnitFileState=static'):
            with self.assertRaisesRegex(m.Refusal, 'shadowed'):
                m.observer_start()
            with self.assertRaisesRegex(m.Refusal, 'shadowed'):
                m.observer_stop()

    def test_route_and_port_validation(self):
        cfg = {'routes': {'demo': {'repo': ROUTE[0], 'pr': ROUTE[1]}},
               'gateway_origin': f'http://127.0.0.1:{m.GATEWAY_PORT}',
               'gateway_credential_file': str(m.GATEWAY), 'ack_actor': 'example-actor',
               'control_port': 18765, 'managed_unit': m.UNIT,
               'managed_fragment': str(m.UNIT_PATH)}
        self.assertEqual(m.validate_observer_config(cfg), cfg)
        for field, value in [('control_port', m.PORT), ('gateway_origin', 'http://elsewhere:8642'),
                             ('gateway_credential_file', '/other/key'), ('routes', {'demo': {'repo': '../other', 'pr': 42}})]:
            with self.subTest(field=field), self.assertRaises(m.Refusal):
                m.validate_observer_config({**cfg, field: value})

    def test_opt_in_preparation_stays_inactive_and_refuses_changed_artifact(self):
        source_dir = Path(self.tmp.name) / 'reviewed-source'
        source_dir.mkdir(mode=0o700)
        (source_dir / 'watchdog.py').write_bytes(Path(w.__file__).read_bytes())
        cfg = {'routes': {'demo': {'repo': ROUTE[0], 'pr': ROUTE[1]}},
               'gateway_origin': f'http://127.0.0.1:{m.GATEWAY_PORT}',
               'gateway_credential_file': str(m.GATEWAY), 'ack_actor': 'example-actor',
               'control_port': 18765, 'managed_unit': m.UNIT,
               'managed_fragment': str(m.UNIT_PATH)}
        candidate = source_dir / 'watchdog-config.json'
        candidate.write_text(json.dumps(cfg))
        candidate.chmod(0o600)
        with (mock.patch.object(m, '__file__', str(source_dir / 'on-demand.py')),
              mock.patch.object(m, 'OBSERVER', m.INSTALL_DIR / 'watchdog.py'),
              mock.patch.object(m, 'OBSERVER_CONFIG', m.ROOT / 'watchdog-config.json')):
            m.install_observer()
            self.assertTrue(m.observer_installed())
            self.assertEqual((m.INSTALL_DIR / 'watchdog.py').read_bytes(), (source_dir / 'watchdog.py').read_bytes())
            self.assertFalse(any(call[0] == 'start' for call in self.calls))
            m.install_observer()
            self.assertFalse(any(call[0] == 'start' for call in self.calls))
            (m.INSTALL_DIR / 'watchdog.py').write_text('changed')
            with self.assertRaises(m.Refusal):
                m.install_observer()


class ControlIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.credential = self.dir / 'gateway'
        self.credential.write_bytes(b'fictional-key')
        self.credential.chmod(0o600)
        self.store = w.Store(self.dir / 'state')
        self.watch = w.Watchdog(self.store, {'demo': ROUTE}, mock.Mock(), clock=lambda: 1000)
        self.port = self.free_port()
        real_server = w.HTTPServer
        self.server = None

        def factory(*args, **kwargs):
            self.server = real_server(*args, **kwargs)
            return self.server

        with mock.patch.object(w, 'HTTPServer', factory):
            self.thread = threading.Thread(target=w.serve_control,
                                           args=(self.watch, self.port, self.credential), daemon=True)
            self.thread.start()
            for _ in range(50):
                if self.server:
                    break
                time.sleep(0.02)
            self.assertIsNotNone(self.server)
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    @staticmethod
    def free_port():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    def request(self, method, path, body=None, token='fictional-key'):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        headers = {'Authorization': 'Bearer ' + token}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    def payload(self):
        return {'project': 'demo', 'repo': ROUTE[0], 'pr': 42, 'run_id': RUN,
                'correlation': 'fictional-1', 'deadline_at': 1601}

    def test_auth_register_read_duplicate_no_task_start(self):
        self.assertEqual(self.request('GET', '/health', token='wrong')[0], 401)
        self.assertEqual(self.request('GET', '/health')[0], 200)
        self.assertEqual(self.request('POST', '/register', self.payload(), token='wrong')[0], 401)
        self.assertEqual(self.request('POST', '/register?mode=register', self.payload())[0], 201)
        self.assertEqual(self.request('GET', '/tracked/' + RUN)[1]['correlation'], 'fictional-1')
        self.assertEqual(self.request('POST', '/register', self.payload())[0], 400)
        self.assertEqual(self.request('GET', '/tracked/' + RUN)[0], 200)
        self.watch.gateway.status.assert_not_called()

    def test_route_and_run_validation_no_adoption(self):
        for changes in ({'repo': 'example/elsewhere'}, {'run_id': 'bad'},
                        {'deadline_at': float('nan')}, {'pr': 43}):
            with self.subTest(changes=changes):
                self.assertEqual(self.request('POST', '/register', {**self.payload(), **changes})[0], 400)
        self.assertEqual(self.request('GET', '/tracked/' + RUN)[0], 404)
        self.assertEqual(self.store.load()['runs'], {})

    def test_isolated_python_cli_uses_explicit_private_config_without_cwd(self):
        config = self.dir / 'watchdog-config.json'
        config.write_text(json.dumps({'routes': {'demo': {'repo': ROUTE[0], 'pr': 42}},
                                      'gateway_origin': 'http://127.0.0.1:8642',
                                      'gateway_credential_file': str(self.credential),
                                      'ack_actor': 'example-actor', 'control_port': self.port,
                                      'managed_unit': 'example.service',
                                      'managed_fragment': str(self.dir / 'example.service')}))
        config.chmod(0o600)
        command = ['/usr/bin/python3', '-I', str(Path(w.__file__).resolve()),
                   '--state-dir', str(self.dir / 'cli-state'), '--config', str(config)]
        for args in (['pause'], ['register', '--project', 'demo', '--repo', ROUTE[0],
                                   '--pr', '42', '--run-id', RUN, '--correlation', 'fictional-1',
                                   '--deadline-at', str(time.time() + 1000)], ['enable']):
            with self.subTest(args=args):
                result = subprocess.run([*command, *args], cwd='/',
                                        env={**os.environ, 'PYTHONPATH': '/nonexistent'},
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(w.Store(self.dir / 'cli-state').load()['runs'][RUN]['repo'], ROUTE[0])


if __name__ == '__main__':
    unittest.main()
