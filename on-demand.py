#!/usr/bin/python3
"""Inactive, user-scoped lifecycle for a locally installed, pinned MCP client."""
import fcntl
import json
import re
import hashlib
import http.client
import os
from pathlib import Path
import pwd
import grp
import socket
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from getpass import getpass

UID = os.getuid()
HOME = Path(pwd.getpwuid(UID).pw_dir)
DEFAULT_CONFIG = HOME / '.config/hermes-mcp-connection/config.json'
SETTINGS_PATH = DEFAULT_CONFIG
# Values below are replaced only after strict local-config validation.
CLIENT = Path('/usr/local/bin/mcp-tunnel-client')
CLIENT_SHA = '0' * 64
CONFIG_SHA = '0' * 64
SOURCE_CONFIG = Path('/etc/hermes-mcp-connection/client.yaml')
UNIT = 'hermes-mcp-connection.service'
PORT = 18764
GATEWAY_PORT = 8642
INSTALL_DIR = HOME / '.local/lib/hermes-mcp-connection'
ROOT = HOME / '.local/state/hermes-mcp-connection'
UNIT_DIR = HOME / '.config/systemd/user'
UNIT_PATH = UNIT_DIR / UNIT
CREDS = ROOT / 'credentials'
RUNTIME = CREDS / 'runtime'
GATEWAY = CREDS / 'gateway'
CONFIG = INSTALL_DIR / 'client.yaml'
INSTALLED = INSTALL_DIR / 'on-demand.py'
LOCK = ROOT / 'operation.lock'
TRANSIENT = Path(f'/run/user/{UID}/credentials/{UNIT}')
OBSERVER = INSTALL_DIR / 'watchdog.py'
OBSERVER_CONFIG = ROOT / 'watchdog-config.json'
OBSERVER_STATE = ROOT / 'watchdog-state'
TIMEOUT = 25
FLAGS = ('--control-plane.poll-channel', 'main', '--control-plane.poll-channel', 'harpoon',
         '--harpoon.additional-transport=http-streamable', '--health.listen-addr',
         f'127.0.0.1:{PORT}', '--harpoon.allow-plaintext-http=false',
         '--harpoon.capture-payloads=false', '--allow-remote-ui=false',
         '--log.http-raw-unsafe=false', '--forward-request-id', 'none')


def load_local_config(path):
    """Validate pinned settings before reading any of their executable paths."""
    if not re.fullmatch(r'/[A-Za-z0-9_./+-]+', str(path)):
        raise Refusal('Settings path must be an absolute simple local path')
    try:
        secure_existing_parents(Path(path))
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            st = os.fstat(fd)
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != UID
                    or stat.S_IMODE(st.st_mode) != 0o600 or st.st_nlink != 1):
                raise Refusal('Unsafe settings ownership, mode or link count')
            with os.fdopen(fd, 'rb') as stream:
                fd = -1
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise Refusal('Settings file too large')
            data = json.loads(raw)
        finally:
            if fd >= 0:
                os.close(fd)
    except (OSError, ValueError, UnicodeError) as exc:
        raise Refusal('Cannot read local JSON configuration') from exc
    required = {'client_path', 'client_sha256', 'client_config_path',
                'client_config_sha256', 'tunnel_id', 'listener_port',
                'gateway_port', 'install_dir', 'state_dir', 'unit_name'}
    if not isinstance(data, dict) or set(data) != required:
        raise Refusal('Configuration fields do not match the supported schema')
    for key in ('client_path', 'client_config_path', 'install_dir', 'state_dir'):
        value = data[key]
        if not isinstance(value, str) or not re.fullmatch(r'/[A-Za-z0-9_./+-]+', value):
            raise Refusal('Paths must be absolute local paths')
    for key in ('client_sha256', 'client_config_sha256'):
        if not isinstance(data[key], str) or not re.fullmatch(r'[0-9a-f]{64}', data[key]):
            raise Refusal('SHA-256 pins must be 64 lowercase hexadecimal characters')
    if not isinstance(data['tunnel_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', data['tunnel_id']):
        raise Refusal('Invalid tunnel identifier')
    for key in ('listener_port', 'gateway_port'):
        if type(data[key]) is not int or not 1 <= data[key] <= 65535:
            raise Refusal('Ports must be integers from 1 through 65535')
    if data['listener_port'] == data['gateway_port']:
        raise Refusal('Listener and gateway ports must differ')
    if not isinstance(data['unit_name'], str) or not re.fullmatch(r'[a-zA-Z0-9_.@-]+[.]service', data['unit_name']):
        raise Refusal('Invalid systemd unit name')
    globals().update(CLIENT=Path(data['client_path']), CLIENT_SHA=data['client_sha256'],
                     SOURCE_CONFIG=Path(data['client_config_path']),
                     CONFIG_SHA=data['client_config_sha256'], PORT=data['listener_port'],
                     GATEWAY_PORT=data['gateway_port'], UNIT=data['unit_name'],
                     INSTALL_DIR=Path(data['install_dir']), ROOT=Path(data['state_dir']))
    global UNIT_PATH, CONFIG, INSTALLED, LOCK, CREDS, RUNTIME, GATEWAY, TRANSIENT, FLAGS, SETTINGS_PATH
    global OBSERVER, OBSERVER_CONFIG, OBSERVER_STATE
    SETTINGS_PATH = Path(path)
    UNIT_PATH = UNIT_DIR / UNIT
    CONFIG = INSTALL_DIR / 'client.yaml'
    INSTALLED = INSTALL_DIR / 'on-demand.py'
    LOCK = ROOT / 'operation.lock'
    OBSERVER = INSTALL_DIR / 'watchdog.py'
    OBSERVER_CONFIG = ROOT / 'watchdog-config.json'
    OBSERVER_STATE = ROOT / 'watchdog-state'
    CREDS = ROOT / 'credentials'
    RUNTIME, GATEWAY = CREDS / 'runtime', CREDS / 'gateway'
    TRANSIENT = Path(f'/run/user/{UID}/credentials/{UNIT}')
    FLAGS = ('--control-plane.poll-channel', 'main', '--control-plane.poll-channel', 'harpoon',
             '--harpoon.additional-transport=http-streamable', '--health.listen-addr',
             f'127.0.0.1:{PORT}', '--harpoon.allow-plaintext-http=false',
             '--harpoon.capture-payloads=false', '--allow-remote-ui=false',
             '--log.http-raw-unsafe=false', '--forward-request-id', 'none')

class Refusal(Exception):
    pass


def check_dir(path, private=False):
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != UID or (st.st_mode & (0o022 if not private else 0o077)):
        raise Refusal('Unsafe directory ownership or permissions: ' + str(path))


def check_file(path, mode=0o600):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != UID or stat.S_IMODE(st.st_mode) != mode or st.st_nlink != 1:
        raise Refusal('Unsafe file ownership, link count or permissions: ' + str(path))


def secure_existing_parents(path):
    """Reject symlinks and writable-by-other-user parents (solo private group allowed)."""
    ancestors = list(path.parents)
    for parent in reversed(ancestors):
        if parent == Path('/'):
            continue
        st = parent.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid not in (0, UID) or (st.st_mode & 0o002):
            raise Refusal('Unsafe ancestor: ' + str(parent))
        if st.st_mode & 0o020:
            members = {p.pw_uid for p in pwd.getpwall() if p.pw_gid == st.st_gid}
            members.update(pwd.getpwnam(n).pw_uid for n in grp.getgrgid(st.st_gid).gr_mem)
            if members - {UID}:
                raise Refusal('Shared writable ancestor: ' + str(parent))


def digest(path):
    if path.is_symlink() or not path.is_file():
        raise Refusal('Missing or linked pinned file: ' + str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_pins(config):
    if digest(CLIENT) != CLIENT_SHA or digest(config) != CONFIG_SHA:
        raise Refusal('Pinned client/configuration bytes have changed')


def ensure_dir(path):
    if path.exists() or path.is_symlink():
        check_dir(path, private=True)
    else:
        path.mkdir(mode=0o700)
        check_dir(path, private=True)


def write_new(path, data, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    check_file(path, mode)


def unit_text():
    return (f'[Unit]\nDescription=On-demand MCP connection client\n'
            f'[Service]\nType=exec\nExecStart=/usr/bin/python3 -I {INSTALLED} --settings {SETTINGS_PATH} serve\n'
            f'LoadCredential=runtime:{RUNTIME}\nLoadCredential=gateway:{GATEWAY}\n'
            f'Restart=no\nTimeoutStopSec=5\n'
            f'KillMode=control-group\nNoNewPrivileges=yes\nUMask=0077\n'
            f'WorkingDirectory={ROOT}\n'
            f'[Install]\n# Intentionally no WantedBy/RequiredBy; never enable this unit.\n')


def observer_names():
    stem = UNIT.removesuffix('.service')
    return stem + '-watchdog.service', stem + '-watchdog.timer', stem + '-control.service'

def observer_paths():
    return tuple(UNIT_DIR / name for name in observer_names())

def observer_command(action):
    return f'/usr/bin/python3 -I {OBSERVER} --state-dir {OBSERVER_STATE} --config {OBSERVER_CONFIG} {action}'

def observer_units(control_port):
    tick, _, _ = observer_names()
    return (
        '[Unit]\nDescription=Bounded MCP watchdog tick\n'
        f'[Service]\nType=oneshot\nExecStart={observer_command("tick")}\n'
        f'TimeoutStartSec=44\nNoNewPrivileges=yes\nUMask=0077\nWorkingDirectory={ROOT}\n',
        '[Unit]\nDescription=Opt-in MCP watchdog timer\n'
        f'[Timer]\nOnActiveSec=60s\nOnUnitActiveSec=60s\nAccuracySec=1s\nUnit={tick}\n'
        '[Install]\n# Static: never enable at boot.\n',
        '[Unit]\nDescription=Authenticated loopback run registration\n'
        f'[Service]\nType=simple\nExecStart={observer_command("serve-control")} '
        f'--port {control_port} --credential-file %d/gateway\n'
        f'LoadCredential=gateway:{GATEWAY}\nRestart=no\nTimeoutStopSec=5\n'
        f'WorkingDirectory={ROOT}\nNoNewPrivileges=yes\nUMask=0077\n'
        '[Install]\n# Static: never enable at boot.\n')

def validate_observer_config(data):
    if (not isinstance(data, dict) or set(data) != {'routes', 'gateway_origin', 'gateway_credential_file', 'ack_actor', 'control_port', 'managed_unit', 'managed_fragment'}
            or data['gateway_origin'] != f'http://127.0.0.1:{GATEWAY_PORT}'
            or data['gateway_credential_file'] != str(GATEWAY)
            or data['managed_unit'] != UNIT or data['managed_fragment'] != str(UNIT_PATH)
            or type(data['control_port']) is not int
            or data['control_port'] in (PORT, GATEWAY_PORT)
            or not 1 <= data['control_port'] <= 65535
            or not isinstance(data['routes'], dict) or not data['routes']
            or not isinstance(data['ack_actor'], str)
            or not re.fullmatch(r'[A-Za-z0-9-]+', data['ack_actor'])):
        raise Refusal('Invalid observer configuration or gateway credential route')
    for project, route in data['routes'].items():
        if (not isinstance(project, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', project)
                or not isinstance(route, dict) or set(route) != {'repo', 'pr'}
                or not isinstance(route['repo'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', route['repo'])
                or any(p in ('.', '..') for p in route['repo'].split('/'))
                or type(route['pr']) is not int or route['pr'] < 1):
            raise Refusal('Invalid observer project route')
    return data

def observer_config():
    check_file(OBSERVER_CONFIG)
    return validate_observer_config(json.loads(OBSERVER_CONFIG.read_text()))

def observer_installed():
    paths = observer_paths()
    if not any(p.exists() or p.is_symlink() for p in (OBSERVER, OBSERVER_CONFIG, *paths)):
        return False
    check_file(OBSERVER)
    config = observer_config()
    for path, text in zip(paths, observer_units(config['control_port'])):
        check_file(path)
        if path.read_text() != text:
            raise Refusal('Observer unit changed or partially installed')
    return True

def observer_action(action):
    p = subprocess.run(['/usr/bin/python3', '-I', str(OBSERVER), '--state-dir',
                        str(OBSERVER_STATE), '--config', str(OBSERVER_CONFIG), action],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
    if p.returncode:
        raise Refusal('Observer ' + action + ' failed')

def check_observer_units():
    for name, path in zip(observer_names(), observer_paths()):
        text = systemctl('show', name, '-p', 'FragmentPath', '-p', 'UnitFileState')
        props = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
        if props.get('FragmentPath') != str(path) or props.get('UnitFileState') not in ('static', 'disabled'):
            raise Refusal('Observer unit shadowed or enabled: ' + name)

def control_ready(port):
    check_file(GATEWAY)
    key = GATEWAY.read_bytes()
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=0.7)
    try:
        connection.request('GET', '/health', headers={'Authorization': 'Bearer ' + key.decode('ascii')})
        return connection.getresponse().status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()

def observer_start():
    if not observer_installed():
        return
    check_observer_units()
    port = observer_config()['control_port']
    try:
        systemctl('start', observer_names()[2])
        end = time.monotonic() + 8
        while time.monotonic() < end and not control_ready(port):
            time.sleep(0.15)
        if not control_ready(port):
            raise Refusal('Registration control not ready')
        observer_action('enable')
        systemctl('start', observer_names()[1])
        if systemctl('is-active', observer_names()[1]) != 'active':
            raise Refusal('Observer timer not active')
    except BaseException:
        observer_stop()
        raise

def observer_stop():
    if not observer_installed():
        return
    check_observer_units()
    # Stop timer and any in-flight tick before taking the registry lock.
    for name in (observer_names()[1], observer_names()[0], observer_names()[2]):
        systemctl('stop', name)
    observer_action('pause')

def install_observer():
    """Opt-in preparation, only while disconnected; never starts units."""
    with locked():
        props = installed_state()
        if props.get('ActiveState') not in ('inactive', 'failed'):
            raise Refusal('Disconnect before installing observer artifacts')
        source = Path(__file__).with_name('watchdog.py')
        candidate = Path(__file__).with_name('watchdog-config.json')
        if source.is_symlink() or candidate.is_symlink():
            raise Refusal('Observer source must not be linked')
        check_file(candidate)
        data = json.loads(candidate.read_text())
        validate_observer_config(data)
        expected = {OBSERVER: source.read_bytes(), OBSERVER_CONFIG: candidate.read_bytes()}
        expected.update({p: t.encode() for p, t in zip(observer_paths(), observer_units(data['control_port']))})
        for path, content in expected.items():
            if path.exists() or path.is_symlink():
                check_file(path)
                if path.read_bytes() != content:
                    raise Refusal('Observer artifact collision: ' + str(path))
        for path, content in expected.items():
            if not path.exists():
                write_new(path, content, 0o600)
        observer_config()
        systemctl('daemon-reload')
        print('Observer prepared inactive; no unit enabled or started.')

def systemctl(*args):
    # No inherited credential/debug/profile environment in service-control subprocesses.
    env = {'HOME': str(HOME), 'PATH': '/usr/bin:/bin',
           'XDG_RUNTIME_DIR': f'/run/user/{UID}'}
    p = subprocess.run(['/usr/bin/systemctl', '--user', *args], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       text=True, timeout=12)
    if p.returncode:
        raise Refusal('User service control failed: ' + args[0])
    return p.stdout.strip()


def properties():
    text = systemctl('show', UNIT, '-p', 'ActiveState', '-p', 'SubState',
                     '-p', 'MainPID', '-p', 'FragmentPath', '-p', 'UnitFileState',
                     '-p', 'ActiveEnterTimestampMonotonic')
    return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)


def check_unit():
    check_file(UNIT_PATH, 0o600)
    if UNIT_PATH.read_text() != unit_text():
        raise Refusal('Unit collision or changed managed unit')
    props = properties()
    if props.get('FragmentPath') != str(UNIT_PATH) or props.get('UnitFileState') not in ('static', 'disabled'):
        raise Refusal('Unit shadowed, enabled or not loaded from installed path')
    return props


def listener_inode():
    """Linux /proc TCP listening socket inode, not a guessed PID from a port."""
    for line in Path('/proc/net/tcp').read_text().splitlines()[1:]:
        parts = line.split()
        if parts[1] == f'0100007F:{PORT:04X}' and parts[3] == '0A':
            return parts[9]
    return None


def pid_owns_listener(pid):
    if not pid or pid <= 0:
        return False
    try:
        if not os.path.samefile(f'/proc/{pid}/exe', CLIENT):
            return False
        argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        if b'run' not in argv or str(CONFIG).encode() not in argv:
            return False
        inode = listener_inode()
        if not inode:
            return False
        return any(os.readlink(fd) == f'socket:[{inode}]'
                   for fd in Path(f'/proc/{pid}/fd').iterdir())
    except (OSError, ValueError):
        return False


def ready(pid):
    if not pid_owns_listener(pid):
        return False
    try:
        connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=0.7)
        connection.request('GET', '/readyz')
        response = connection.getresponse()
        code = response.status
        response.read(1024)
        connection.close()
        return code == 200 and pid_owns_listener(pid)
    except (OSError, http.client.HTTPException):
        return False


def other_client_running():
    """Refuse any other process using the pinned executable; never signal it."""
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if int(entry.name) != os.getpid() and os.path.samefile(entry / 'exe', CLIENT):
                return True
        except OSError:
            pass
    return False


def port_free():
    """Probe with the same TCP reuse/listen semantics as Go's net.Listen."""
    try:
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', PORT))
            sock.listen(1)
        return True
    except OSError:
        return False


@contextmanager
def locked():
    secure_existing_parents(ROOT)
    ensure_dir(ROOT)
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        check_file(LOCK)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def installed_state():
    check_dir(ROOT, private=True)
    check_dir(CREDS, private=True)
    for path in (RUNTIME, GATEWAY, CONFIG, INSTALLED):
        check_file(path)
    verify_pins(CONFIG)
    return check_unit()


def setup():
    # Strictly verify the local, non-secret configuration before any host action.
    load_local_config(SETTINGS_PATH)
    with locked():
        # Verify the pinned source YAML matches its local hash before install.
        if digest(SOURCE_CONFIG) != CONFIG_SHA:
            raise Refusal('Pinned client configuration bytes have changed')
        # The example config must not be installed with the fictional tunnel id.
        config_bytes = SOURCE_CONFIG.read_bytes()
        if b'tunnel_example_replace_me' in config_bytes:
            raise Refusal('Refusing to install the fictional example tunnel identifier')
        secure_existing_parents(UNIT_DIR)
        if UNIT_DIR.exists() or UNIT_DIR.is_symlink():
            check_dir(UNIT_DIR, private=True)
        else:
            UNIT_DIR.mkdir(mode=0o700)
        secure_existing_parents(INSTALL_DIR)
        if INSTALL_DIR.exists() or INSTALL_DIR.is_symlink():
            check_dir(INSTALL_DIR, private=True)
        else:
            INSTALL_DIR.mkdir(mode=0o700)
            check_dir(INSTALL_DIR, private=True)
        existing = properties()
        if existing.get('FragmentPath') and existing['FragmentPath'] != str(UNIT_PATH):
            raise Refusal('Unit name collision outside this package')
        if existing.get('ActiveState') not in ('inactive', 'failed'):
            raise Refusal('Setup refuses an active or unknown unit state')
        ensure_dir(CREDS)
        # Preflight *all* non-credential collisions before prompting or writing.
        expected = {CONFIG: SOURCE_CONFIG.read_bytes(), INSTALLED: Path(__file__).read_bytes(),
                    UNIT_PATH: unit_text().encode()}
        for path, data in expected.items():
            if path.exists() or path.is_symlink():
                check_file(path)
                if path.read_bytes() != data:
                    raise Refusal('Existing managed artifact differs: ' + str(path))
        present = [p.exists() or p.is_symlink() for p in (RUNTIME, GATEWAY)]
        if present[0] != present[1]:
            raise Refusal('Partial credential pair: repair privately before retrying')
        if all(present):
            for path in (RUNTIME, GATEWAY):
                check_file(path)
            print('Credentials retained; setup is idempotent (no key read or prompt).')
        else:
            if not sys.stdin.isatty() or not sys.stderr.isatty():
                raise Refusal('Private interactive terminal required for first setup')
            values = [getpass('OpenAI tunnel API key (restricted to Tunnels Read/Use): '), getpass('Hermes gateway API key (without Bearer prefix): ')]
            if any(not value or any(not 33 <= ord(c) <= 126 for c in value)
                   for value in values):
                raise Refusal('Keys must be nonempty single-line printable ASCII without whitespace')
            # Neither key is command-line text, shell code, unit content or a log entry.
            try:
                write_new(RUNTIME, values[0].encode(), 0o600)
                write_new(GATEWAY, values[1].encode(), 0o600)
            except BaseException:
                RUNTIME.unlink(missing_ok=True)
                GATEWAY.unlink(missing_ok=True)
                raise
            del values
        for path, data in expected.items():
            if not path.exists():
                write_new(path, data, 0o600)
        systemctl('daemon-reload')
        props = check_unit()
        if props.get('ActiveState') not in ('inactive', 'failed'):
            raise Refusal('Setup does not alter an active service')
        print('Installed inactive; never enabled. Keys are owner-only files, NOT encrypted.')


def wait_stopped():
    end = time.monotonic() + 8
    while time.monotonic() < end:
        props = properties()
        if (props.get('ActiveState') in ('inactive', 'failed') and not listener_inode()
                and not TRANSIENT.exists()
                and not (Path('/run/credentials') / UNIT).exists()):
            return True
        time.sleep(0.15)
    return False


def disconnect_inner():
    systemctl('stop', UNIT)  # Stop this unit and remove LoadCredential transient copies.
    if not wait_stopped():
        raise Refusal('Managed stop/listener cleanup unconfirmed; do not assume disconnected')


def connect():
    with locked():
        props = installed_state()
        linger = subprocess.run(['/usr/bin/loginctl', 'show-user', str(UID), '-p', 'Linger', '--value'],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                timeout=8).stdout.strip()
        if linger != 'yes':
            raise Refusal('User lingering is not enabled: service may die on logout; no change made')
        state = props.get('ActiveState')
        pid = int(props.get('MainPID', '0'))
        if state in ('active', 'activating'):
            if ready(pid):
                observer_start()
                print('ready (already connected)')
                return
            raise Refusal('Existing managed service is not ready; inspect status, then disconnect')
        if other_client_running():
            raise Refusal('Pinned client already running; no process stopped')
        if listener_inode():
            raise Refusal('Loopback listener occupied; no process stopped')
        if not port_free():
            raise Refusal('Loopback bind/listen unavailable (no listener identified); no process stopped')
        if state not in ('inactive', 'failed'):
            raise Refusal('Unknown service state; refusing start')
        if state == 'failed':
            systemctl('reset-failed', UNIT)
        try:
            systemctl('start', UNIT)
            end = time.monotonic() + TIMEOUT
            while time.monotonic() < end:
                props = properties()
                pid = int(props.get('MainPID', '0'))
                if props.get('ActiveState') == 'active' and ready(pid):
                    observer_start()
                    print('ready (managed /readyz HTTP 200)')
                    return
                if props.get('ActiveState') == 'failed':
                    break
                time.sleep(0.2)
            raise Refusal('Managed connection did not become ready within the deadline')
        except BaseException:
            # Never leave a possibly exposed session after reporting connect failure.
            disconnect_inner()
            raise


def status():
    with locked():
        if not UNIT_PATH.exists() and not UNIT_PATH.is_symlink():
            print('unknown (unmanaged listener)' if listener_inode() else 'disconnected (not installed)')
            return
        try:
            props = installed_state()
            if props.get('ActiveState') == 'active':
                int(props.get('MainPID', '0'))
                int(props.get('ActiveEnterTimestampMonotonic', '0'))
        except (Refusal, OSError, ValueError, subprocess.TimeoutExpired):
            print('unknown (cannot verify managed unit/configuration)')
            return
        state = props.get('ActiveState')
        if state == 'failed':
            result = 'failed'
        elif state in ('activating',):
            result = 'starting'
        elif state == 'active':
            pid = int(props.get('MainPID', '0'))
            since = int(props.get('ActiveEnterTimestampMonotonic', '0'))
            age = time.monotonic() - since / 1_000_000 if since else TIMEOUT + 1
            result = ('ready' if ready(pid) else
                      'starting' if pid > 0 and age <= TIMEOUT else 'unknown')
        elif state == 'inactive' and not listener_inode():
            result = 'disconnected'
        else:
            result = 'unknown'
        print(result)


def disconnect():
    with locked():
        # Suppress all future notifications and drain an in-flight tick before
        # stopping the tunnel. Never interpret this as cancelling a job.
        observer_stop()
        if not UNIT_PATH.exists() and not UNIT_PATH.is_symlink():
            if listener_inode():
                raise Refusal('Unmanaged listener present; nothing stopped')
            print('disconnected (not installed)')
            return
        check_unit()
        disconnect_inner()
        print('disconnected; retained credentials remain. Existing client tasks may continue; this is not task cancellation or credential revocation.')


def uninstall():
    with locked():
        if observer_installed():
            raise Refusal('Observer installed; retain its state and remove only via reviewed rollback')
        if not UNIT_PATH.exists() and not UNIT_PATH.is_symlink():
            print('Already uninstalled; retained credentials untouched.')
            return
        props = check_unit()
        if props.get('ActiveState') not in ('inactive', 'failed') or listener_inode():
            raise Refusal('Disconnect and verify listener absence before uninstall')
        verify_pins(CONFIG)
        for path, data in ((CONFIG, CONFIG.read_bytes()),
                           (INSTALLED, Path(__file__).read_bytes()),
                           (UNIT_PATH, unit_text().encode())):
            check_file(path)
            if path.read_bytes() != data:
                raise Refusal('Changed artifact; refusing partial uninstall')
        for path in (UNIT_PATH, CONFIG, INSTALLED):
            path.unlink()
        systemctl('daemon-reload')
        print('Uninstalled unit and managed code/config; retained credentials and lock untouched.')


def serve():
    # Run by the user service only. systemd copies LoadCredential into a private,
    # transient credential directory and destroys the copy on unit stop.
    cred_dir = os.environ.get('CREDENTIALS_DIRECTORY', '')
    expected = (f'/run/user/{UID}/credentials/{UNIT}', f'/run/credentials/{UNIT}')
    if cred_dir not in expected:
        raise Refusal('Missing expected systemd credential directory')
    verify_pins(CONFIG)
    values = []
    for name in ('runtime', 'gateway'):
        path = Path(cred_dir) / name
        if path.is_symlink() or not path.is_file():
            raise Refusal('Missing or linked transient credential')
        value = path.read_bytes()
        if not value or any(c < 33 or c > 126 for c in value):
            raise Refusal('Invalid transient credential format')
        values.append(value.decode('ascii'))
    argv = [str(CLIENT), 'run', '--config', str(CONFIG), *FLAGS]
    os.execve(CLIENT, argv, {'HOME': str(HOME), 'PATH': '/usr/bin:/bin',
                              'CONTROL_PLANE_API_KEY': values[0],
                              'HERMES_API_AUTHORIZATION': 'Bearer ' + values[1]})


def main():
    args = sys.argv[1:]
    if len(args) == 3 and args[0] == '--settings':
        global SETTINGS_PATH
        SETTINGS_PATH = Path(args[1])
        load_local_config(SETTINGS_PATH)
        args = args[2:]
    elif len(args) == 1:
        load_local_config(DEFAULT_CONFIG)
    else:
        args = None
    if args is None or len(args) != 1 or args[0] not in ('setup', 'install-observer', 'connect', 'status', 'disconnect', 'uninstall', 'serve'):
        raise Refusal('Usage: on-demand.py [--settings FILE] {setup|install-observer|connect|status|disconnect|uninstall}')
    os.umask(0o077)
    globals()[args[0].replace('-', '_')]()

if __name__ == '__main__':
    try:
        main()
    except (Refusal, OSError, subprocess.TimeoutExpired) as exc:
        print('Refused: ' + str(exc), file=sys.stderr)
        sys.exit(1)
