"""Offline fictional-key lifecycle tests. Never call the host's real systemctl/client."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

MODULE = Path(__file__).with_name('on-demand.py')
spec = importlib.util.spec_from_file_location('on_demand', MODULE)
assert spec is not None and spec.loader is not None
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
REAL_LISTENER = m.listener_inode
REAL_PID_OWNER = m.pid_owns_listener
REAL_READY = m.ready


class FakeManager:
    def __init__(self):
        self.state = 'inactive'
        self.calls = []
        self.started = 0
        self.stopped = 0
        self.ready_after = 0
        self.fragment = ''
        self.active_since = 0

    def call(self, *args):
        self.calls.append(args)
        cmd = args[0]
        if cmd == 'daemon-reload':
            self.fragment = str(m.UNIT_PATH) if m.UNIT_PATH.exists() else ''
        elif cmd == 'show':
            return (f'ActiveState={self.state}\nSubState=running\n'
                    f'MainPID={424242 if self.state == "active" else 0}\n'
                    f'FragmentPath={self.fragment}\n'
                    f'UnitFileState={"static" if self.fragment else ""}\n'
                    f'ActiveEnterTimestampMonotonic={self.active_since}')
        elif cmd == 'start':
            self.started += 1
            self.state = 'active'
            self.active_since = int(time.monotonic() * 1_000_000)
        elif cmd == 'stop':
            self.stopped += 1
            self.state = 'inactive'
        elif cmd == 'reset-failed':
            # A manager may reject reset-failed for an inactive, unloaded unit.
            if self.state != 'failed':
                raise m.Refusal(f'Unit {m.UNIT} not loaded.')
            self.state = 'inactive'
        else:
            raise AssertionError(args)
        return ''


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'home'
        (self.home / '.local/state').mkdir(parents=True, mode=0o700)
        (self.home / '.local/lib').mkdir(parents=True, mode=0o700)
        (self.home / '.config/systemd').mkdir(parents=True, mode=0o700)
        self.source = Path(self.tmp.name) / 'source.yaml'
        self.source.write_text('fictional configuration\n')
        self.client = Path(self.tmp.name) / 'client'
        self.client.write_text('fictional stub client\n')
        self.config_dir = Path(self.tmp.name) / 'config'
        self.config_dir.mkdir(mode=0o700)
        self.settings = self.config_dir / 'config.json'
        self.manager = FakeManager()
        settings = {
            'client_path': str(self.client),
            'client_sha256': hashlib.sha256(self.client.read_bytes()).hexdigest(),
            'client_config_path': str(self.source),
            'client_config_sha256': hashlib.sha256(self.source.read_bytes()).hexdigest(),
            'tunnel_id': 'tunnel_fictional_example',
            'listener_port': self.free_port(),
            'gateway_port': 8642,
            'install_dir': str(self.home / '.local/lib/hermes-mcp-connection'),
            'state_dir': str(self.home / '.local/state/hermes-mcp-connection'),
            'unit_name': m.UNIT,
        }
        self.settings.write_text(json.dumps(settings))
        self.settings.chmod(0o600)
        changes = {
            'HOME': self.home, 'SOURCE_CONFIG': self.source, 'CLIENT': self.client,
            'TIMEOUT': 0.4,
            'TRANSIENT': Path(self.tmp.name) / 'fictional-transient',
            'UNIT_DIR': self.home / '.config/systemd/user',
            'ROOT': self.home / '.local/state/hermes-mcp-connection',
            'INSTALL_DIR': self.home / '.local/lib/hermes-mcp-connection',
            'DEFAULT_CONFIG': self.settings,
            'SETTINGS_PATH': self.settings,
        }
        changes.update(UNIT_PATH=changes['UNIT_DIR'] / m.UNIT,
                       CREDS=changes['ROOT'] / 'credentials',
                       RUNTIME=changes['ROOT'] / 'credentials/runtime',
                       GATEWAY=changes['ROOT'] / 'credentials/gateway',
                       CONFIG=changes['INSTALL_DIR'] / 'client.yaml',
                       INSTALLED=changes['INSTALL_DIR'] / 'on-demand.py',
                       LOCK=changes['ROOT'] / 'operation.lock')
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name, value in changes.items():
            stack.enter_context(mock.patch.object(m, name, value))
        stack.enter_context(mock.patch.object(m, 'systemctl', self.manager.call))
        stack.enter_context(mock.patch.object(m, 'other_client_running', return_value=False))
        stack.enter_context(mock.patch.object(m, 'listener_inode', return_value=None))
        stack.enter_context(mock.patch.object(m, 'ready', side_effect=lambda pid: self.manager.state == 'active' and pid == 424242))
        stack.enter_context(mock.patch.object(m.subprocess, 'run', return_value=mock.Mock(stdout='yes')))
        stack.enter_context(mock.patch.object(m.sys.stdin, 'isatty', return_value=True))
        stack.enter_context(mock.patch.object(m.sys.stderr, 'isatty', return_value=True))
        stack.enter_context(mock.patch.object(m, 'getpass', side_effect=['fictional-runtime', 'fictional-gateway']))
        m.setup()

    def test_custom_settings_survive_setup_without_default(self):
        custom = self.config_dir / 'custom.json'
        custom.write_bytes(self.settings.read_bytes())
        custom.chmod(0o600)
        self.settings.unlink()
        with mock.patch.object(m, 'SETTINGS_PATH', custom):
            m.UNIT_PATH.write_text(m.unit_text())
            m.setup()
            self.assertIn('--settings ' + str(custom) + ' serve', m.UNIT_PATH.read_text())
            self.assertEqual(m.SETTINGS_PATH, custom)

    @staticmethod
    def free_port():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    def test_setup_credentials_private_idempotent_no_enable(self):
        for path in (m.RUNTIME, m.GATEWAY, m.CONFIG, m.INSTALLED, m.UNIT_PATH):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(m.CREDS.stat().st_mode & 0o777, 0o700)
        self.assertNotIn('fictional-runtime', m.UNIT_PATH.read_text())
        self.assertNotIn('fictional-gateway', m.UNIT_PATH.read_text())
        self.assertNotIn('WantedBy=', m.UNIT_PATH.read_text())
        self.assertFalse(any('enable' in call or 'start' in call for call in self.manager.calls))
        with mock.patch.object(m, 'getpass', side_effect=AssertionError('unexpected prompt')):
            m.setup()
        self.assertEqual(m.RUNTIME.read_bytes(), b'fictional-runtime')
        self.assertEqual(m.GATEWAY.read_bytes(), b'fictional-gateway')

    def test_connect_ready_duplicate_and_disconnect_no_restart(self):
        m.connect()
        m.connect()
        self.assertEqual(self.manager.started, 1)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.status()
        self.assertEqual(buf.getvalue().strip(), 'ready')
        m.disconnect()
        m.disconnect()
        self.assertEqual(self.manager.stopped, 2)
        self.assertEqual(self.manager.started, 1)
        self.assertEqual(m.RUNTIME.read_bytes(), b'fictional-runtime')
        self.assertEqual(m.GATEWAY.read_bytes(), b'fictional-gateway')

    def test_crash_after_ready_requires_explicit_reconnect(self):
        # A later crash happens after connect's readiness deadline has ended.
        self.assertIn('Restart=no\n', m.UNIT_PATH.read_text())
        m.connect()
        self.manager.state = 'failed'  # Fake manager: process exited; no listener remains.
        with contextlib.redirect_stdout(io.StringIO()) as output:
            m.status()
        self.assertEqual(output.getvalue().strip(), 'failed')
        self.assertEqual(self.manager.started, 1)
        m.connect()  # A new explicit request is the only way to start again.
        self.assertEqual(self.manager.started, 2)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            m.status()
        self.assertEqual(output.getvalue().strip(), 'ready')

    def test_initial_connect_skips_reset_failed_but_failed_reconnect_uses_it(self):
        # Setup has installed the file; fake manager is inactive/unloaded until start.
        self.assertEqual(self.manager.state, 'inactive')
        m.connect()
        self.assertEqual(self.manager.started, 1)
        self.assertNotIn(('reset-failed', m.UNIT), self.manager.calls)
        self.manager.state = 'failed'
        m.connect()
        self.assertEqual(self.manager.started, 2)
        self.assertEqual(self.manager.calls.count(('reset-failed', m.UNIT)), 1)
        self.assertEqual(self.manager.state, 'active')

    def test_readiness_failure_cleans_managed_service(self):
        with mock.patch.object(m, 'ready', return_value=False):
            with self.assertRaisesRegex(m.Refusal, 'ready'):
                m.connect()
        self.assertEqual(self.manager.state, 'inactive')
        self.assertEqual(self.manager.stopped, 1)
        self.assertEqual(self.manager.started, 1)

    def test_transient_credential_cleanup_required(self):
        transient = Path(self.tmp.name) / 'transient'
        transient.mkdir()
        with mock.patch.object(m, 'TRANSIENT', transient):
            with self.assertRaisesRegex(m.Refusal, 'cleanup unconfirmed'):
                m.disconnect()
            transient.rmdir()
            m.disconnect()
        self.assertEqual(m.RUNTIME.read_bytes(), b'fictional-runtime')

    def test_occupied_listener_is_not_stopped(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', m.PORT))
            sock.listen()
            with mock.patch.object(m, 'listener_inode', REAL_LISTENER):
                with self.assertRaisesRegex(m.Refusal, 'Loopback listener occupied'):
                    m.connect()
            self.assertEqual(self.manager.stopped, 0)
            self.assertEqual(self.manager.started, 0)

    def test_bind_unavailable_without_listener_is_distinguished(self):
        # A bound but non-listening socket without SO_REUSEADDR blocks bind;
        # it is not evidence of an active listener or a particular TCP state.
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', m.PORT))
            with mock.patch.object(m, 'listener_inode', REAL_LISTENER):
                with self.assertRaisesRegex(m.Refusal, r'bind/listen unavailable \(no listener identified\)'):
                    m.connect()
            self.assertEqual(self.manager.started, 0)
            self.assertEqual(self.manager.stopped, 0)

    def test_duplicate_foreground_refused_without_stop(self):
        with mock.patch.object(m, 'other_client_running', return_value=True):
            with self.assertRaisesRegex(m.Refusal, 'already running'):
                m.connect()
        self.assertEqual(self.manager.stopped, 0)

    def test_concurrent_connect_serialized(self):
        errors = []
        def action():
            try:
                m.connect()
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=action) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(self.manager.started, 1)

    def test_uninstall_scope_retains_credentials_and_project(self):
        m.uninstall()
        m.uninstall()
        self.assertFalse(m.UNIT_PATH.exists())
        self.assertFalse(m.INSTALLED.exists())
        self.assertFalse(m.CONFIG.exists())
        self.assertEqual(m.RUNTIME.read_bytes(), b'fictional-runtime')
        self.assertEqual(m.GATEWAY.read_bytes(), b'fictional-gateway')
        self.assertEqual(self.source.read_text(), 'fictional configuration\n')
        self.assertTrue(m.LOCK.exists())
        self.assertEqual(self.manager.started, 0)

    def test_unsafe_permissions_symlink_and_collision_refused(self):
        m.RUNTIME.chmod(0o644)
        with self.assertRaises(m.Refusal):
            m.connect()
        m.RUNTIME.chmod(0o600)
        m.UNIT_PATH.unlink()
        m.UNIT_PATH.symlink_to(self.source)
        with self.assertRaises(m.Refusal):
            m.setup()
        self.assertEqual(self.source.read_text(), 'fictional configuration\n')

    def test_linger_missing_refused(self):
        with mock.patch.object(m.subprocess, 'run', return_value=mock.Mock(stdout='no')):
            with self.assertRaisesRegex(m.Refusal, 'lingering'):
                m.connect()
        self.assertEqual(self.manager.started, 0)

    def test_status_states_and_unknown_without_secret(self):
        buf = io.StringIO()
        self.manager.state = 'failed'
        with contextlib.redirect_stdout(buf):
            m.status()
        self.assertEqual(buf.getvalue().strip(), 'failed')
        self.manager.state = 'activating'
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.status()
        self.assertEqual(buf.getvalue().strip(), 'starting')
        m.UNIT_PATH.chmod(0o644)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.status()
        self.assertIn('unknown', buf.getvalue())
        self.assertNotIn('fictional-', buf.getvalue())

    def test_malformed_manager_pid_is_unknown(self):
        self.manager.state = 'active'
        original = self.manager.call
        def malformed(*args):
            text = original(*args)
            return text.replace('MainPID=424242', 'MainPID=not-a-pid') if args[0] == 'show' else text
        with mock.patch.object(m, 'systemctl', malformed), contextlib.redirect_stdout(io.StringIO()) as output:
            m.status()
        self.assertIn('unknown', output.getvalue())

    def test_malicious_key_not_shell_code_or_argv(self):
        # A key with shell metacharacters is data; control/whitespace is refused.
        with mock.patch.object(m, 'getpass', side_effect=['$(id);`no`', 'key;$(whoami)']):
            m.RUNTIME.unlink()
            m.GATEWAY.unlink()
            m.setup()
        self.assertEqual(m.RUNTIME.read_text(), '$(id);`no`')
        self.assertNotIn('$(id)', m.UNIT_PATH.read_text())
        self.assertNotIn('$(id)', m.INSTALLED.read_text())
        with mock.patch.object(m, 'getpass', side_effect=['line\nmalicious', 'ok']):
            m.RUNTIME.unlink()
            m.GATEWAY.unlink()
            with self.assertRaisesRegex(m.Refusal, 'single-line'):
                m.setup()
        self.assertFalse(m.RUNTIME.exists())

    def test_serve_only_minimal_environment_and_exact_flags(self):
        # Service entry path is exercised with fictional transient files, never exec real client.
        creds_dir = Path(f'/run/user/{m.UID}/credentials/{m.UNIT}')
        with mock.patch.object(m, 'verify_pins') as pins, \
             mock.patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': str(creds_dir),
                                          'OPENAI_API_KEY': 'unrelated-secret',
                                          'HARPOON_DEBUG': 'true'}), \
             mock.patch.object(m.Path, 'is_file', return_value=True), \
             mock.patch.object(m.Path, 'is_symlink', return_value=False), \
             mock.patch.object(m.Path, 'read_bytes', autospec=True, side_effect=lambda p: b'fictional-runtime' if p.name == 'runtime' else b'fictional-gateway'), \
             mock.patch.object(m.os, 'execve', side_effect=RuntimeError('intercepted')) as execve:
            with self.assertRaisesRegex(RuntimeError, 'intercepted'):
                m.serve()
        pins.assert_called_once()
        argv = execve.call_args.args[1]
        env = execve.call_args.args[2]
        self.assertEqual(argv[1:4], ['run', '--config', str(m.CONFIG)])
        self.assertEqual(tuple(argv[4:]), m.FLAGS)
        self.assertEqual(env['HERMES_API_AUTHORIZATION'], 'Bearer fictional-gateway')
        self.assertNotIn('fictional-', str(argv))
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('HARPOON_DEBUG', env)

    def test_uninstall_refuses_active_and_changed_unit(self):
        self.manager.state = 'active'
        with self.assertRaises(m.Refusal):
            m.uninstall()
        self.manager.state = 'inactive'
        m.UNIT_PATH.write_text('foreign configuration')
        with self.assertRaises(m.Refusal):
            m.uninstall()
        self.assertTrue(m.UNIT_PATH.exists())
        self.assertTrue(m.RUNTIME.exists())


class LocalConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(self.tmp.cleanup)
        self.client = Path(self.tmp.name) / 'client'
        self.client.write_text('fictional stub client\n')
        self.config_file = Path(self.tmp.name) / 'client.yaml'
        self.config_file.write_text('fictional tunnel configuration\n')
        self.valid = {
            'client_path': str(self.client),
            'client_sha256': hashlib.sha256(self.client.read_bytes()).hexdigest(),
            'client_config_path': str(self.config_file),
            'client_config_sha256': hashlib.sha256(self.config_file.read_bytes()).hexdigest(),
            'tunnel_id': 'tunnel_fictional_example',
            'listener_port': 28764,
            'gateway_port': 8642,
            'install_dir': str(Path(self.tmp.name) / 'install'),
            'state_dir': str(Path(self.tmp.name) / 'state'),
            'unit_name': 'hermes-mcp-connection.service',
        }

    def write(self, **overrides):
        data = dict(self.valid)
        data.update(overrides)
        path = Path(self.tmp.name) / 'settings.json'
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        return path

    def test_missing_field_refused(self):
        data = dict(self.valid)
        del data['tunnel_id']
        path = Path(self.tmp.name) / 'missing-field.json'
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        with self.assertRaisesRegex(m.Refusal, 'schema'):
            m.load_local_config(path)

    def test_valid_config_loads_derived_paths(self):
        path = self.write()
        m.load_local_config(path)
        self.assertEqual(str(m.CLIENT), self.valid['client_path'])
        self.assertEqual(m.CLIENT_SHA, self.valid['client_sha256'])
        self.assertEqual(m.PORT, 28764)
        self.assertEqual(m.UNIT, 'hermes-mcp-connection.service')
        self.assertIn('127.0.0.1:28764', m.FLAGS)

    def test_schema_hash_port_and_unit_refused(self):
        cases = [
            ({'client_path': 'relative/path'}, 'absolute'),
            ({'client_path': '/safe/path;injected'}, 'absolute'),
            ({'state_dir': '/safe/path with space'}, 'absolute'),
            ({'client_sha256': 'Z' * 64}, 'hexadecimal'),
            ({'tunnel_id': 'bad id with spaces'}, 'tunnel identifier'),
            ({'listener_port': '28764'}, 'integers'),
            ({'listener_port': 0}, '1 through 65535'),
            ({'gateway_port': 28764}, 'must differ'),
            ({'unit_name': 'not-a-unit'}, 'unit name'),
        ]
        for overrides, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(m.Refusal, expected):
                    m.load_local_config(self.write(**overrides))

    def test_unreadable_config_refused(self):
        with self.assertRaisesRegex(m.Refusal, 'configuration'):
            m.load_local_config(Path(self.tmp.name) / 'missing.json')
        bad = Path(self.tmp.name) / 'bad.json'
        bad.write_text('{not json')
        bad.chmod(0o600)
        with self.assertRaisesRegex(m.Refusal, 'configuration'):
            m.load_local_config(bad)

    def test_settings_file_and_ancestors_must_be_owned_and_safe(self):
        path = self.write()
        path.chmod(0o666)
        with self.assertRaisesRegex(m.Refusal, 'Unsafe settings'):
            m.load_local_config(path)
        path.chmod(0o600)
        link = path.with_name('link.json')
        link.symlink_to(path)
        with self.assertRaises(m.Refusal):
            m.load_local_config(link)
        parent = path.with_name('nested')
        parent.mkdir(mode=0o700)
        child = parent / 'config.json'
        child.write_bytes(path.read_bytes())
        child.chmod(0o600)
        parent.chmod(0o777)
        with self.assertRaisesRegex(m.Refusal, 'ancestor'):
            m.load_local_config(child)

    def test_example_has_exact_supported_shape(self):
        example = Path(__file__).with_name('config.example.json')
        data = json.loads(example.read_text())
        path = Path(self.tmp.name) / 'example.json'
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        m.load_local_config(path)

    def test_isolated_entrypoint_checks_selected_settings_before_serve(self):
        path = self.write()
        command = ['/usr/bin/python3', '-I', str(MODULE), '--settings', str(path), 'serve']
        result = subprocess.run(command, cwd='/', capture_output=True, text=True, timeout=5)
        self.assertIn('Missing expected systemd credential directory', result.stderr)
        path.chmod(0o666)
        result = subprocess.run(command, cwd='/', capture_output=True, text=True, timeout=5)
        self.assertIn('Unsafe settings', result.stderr)


class DisposableProcess(unittest.TestCase):
    def test_port_probe_accepts_server_time_wait_like_pinned_listener(self):
        # The server closes the accepted connection first, leaving its local
        # port in TIME_WAIT. This uses only a disposable loopback port.
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(('127.0.0.1', 0))
            port = server.getsockname()[1]
            server.listen()
            with socket.socket() as client:
                client.connect(('127.0.0.1', port))
                accepted, _ = server.accept()
                with accepted:
                    client.sendall(b'x')
                    self.assertEqual(accepted.recv(1), b'x')
                self.assertEqual(client.recv(1), b'')
        rows = (line.split() for line in Path('/proc/net/tcp').read_text().splitlines()[1:])
        self.assertTrue(any(row[1] == f'0100007F:{port:04X}' and row[3] == '06'
                            for row in rows), 'server-side TIME_WAIT fixture not established')
        with mock.patch.object(m, 'PORT', port):
            self.assertTrue(m.port_free(), 'Go-style listener can bind despite TIME_WAIT')

    def test_port_probe_rejects_reuseaddr_listener(self):
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(('127.0.0.1', 0))
            port = server.getsockname()[1]
            with mock.patch.object(m, 'PORT', port):
                server.listen()
                self.assertFalse(m.port_free(), 'real listener must still block the probe')

    def test_actual_stub_http_listener_attributed_to_exact_pid(self):
        # No real client/service: separate Python HTTP server on an ephemeral port.
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as folder:
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            config = Path(folder) / 'fictional.yaml'
            config.write_text('fictional\n')
            script = ('import sys; from http.server import HTTPServer,BaseHTTPRequestHandler; '
                      'H=type("H",(BaseHTTPRequestHandler,),'
                      '{"do_GET":lambda s:(s.send_response(200),s.end_headers(),s.wfile.write(b"ready")),'
                      '"log_message":lambda *a:None}); '
                      'HTTPServer(("127.0.0.1",int(sys.argv[-1])),H).serve_forever()')
            proc = subprocess.Popen([sys.executable, '-c', script, 'run', '--config', str(config), str(port)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                with mock.patch.object(m, 'CLIENT', Path(sys.executable)), \
                     mock.patch.object(m, 'CONFIG', config), \
                     mock.patch.object(m, 'PORT', port), \
                     mock.patch.object(m, 'listener_inode', REAL_LISTENER), \
                     mock.patch.object(m, 'pid_owns_listener', REAL_PID_OWNER), \
                     mock.patch.object(m, 'ready', REAL_READY):
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and not m.ready(proc.pid):
                        time.sleep(0.03)
                    self.assertTrue(m.pid_owns_listener(proc.pid))
                    self.assertTrue(m.ready(proc.pid))
                    self.assertFalse(m.ready(os.getpid()))
            finally:
                proc.terminate()
                proc.wait(timeout=3)

    def test_detached_process_survives_launcher_exit(self):
        # OS-level disposable process smoke, NOT a user-manager/service test.
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as folder:
            pidfile = Path(folder) / 'pid'
            launcher = ('import subprocess,sys; p=subprocess.Popen([sys.executable,"-c",'
                        '"import time; time.sleep(10)"],start_new_session=True,'
                        'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,'
                        'stderr=subprocess.DEVNULL); open(sys.argv[1],"w").write(str(p.pid))')
            subprocess.run([sys.executable, '-c', launcher, str(pidfile)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pid = int(pidfile.read_text())
            try:
                self.assertTrue(Path(f'/proc/{pid}').exists())
            finally:
                os.kill(pid, 15)


if __name__ == '__main__':
    unittest.main()
