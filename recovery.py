"""Opt-in recovery reminders; never submit, cancel or replay a Hermes run.

One reminder per original event. Ambiguous POSTs are reconciled, never replayed.
No scheduler is installed by this module. Existing watchdog state is read only.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import time

_spec = importlib.util.spec_from_file_location('recovery_watchdog', Path(__file__).with_name('watchdog.py'))
w = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(w)

GRACE = 600
LIFETIME = 86400
ERRORS = (OSError, ValueError, subprocess.SubprocessError, TimeoutError)


def reminder_id(event):
    raw = json.dumps(['hermes-watchdog:recovery', event['id'], event['comment_id'], 1],
                     separators=(',', ':')).encode()
    return hashlib.sha256(raw).hexdigest()


def body(event):
    return (f"<!-- hermes-watchdog:recovery:{reminder_id(event)} -->\n"
            f"Original event: {event['id']}\nOriginal comment: {event['comment_id']}\n"
            f"Project: {event['project']}\nRun: {event['run_id']}\n"
            f"Correlation: {event['correlation']}\n"
            "Recovery reminder only; verify and acknowledge the original event.\n"
            "Do not start/replay a job or acknowledge this reminder.")


class RecoveryStore(w.Store):
    def load(self):
        data = json.loads(w._private_read(self.path, 16 * 1024 * 1024),
                          parse_constant=w._reject_json_constant,
                          parse_float=w._finite_json_float)
        if (not isinstance(data, dict) or data.get('version') != 1
                or type(data.get('enabled')) is not bool
                or not isinstance(data.get('excluded_runs'), list)
                or any(not isinstance(r, str) or not w.RUN_ID.fullmatch(r)
                       for r in data['excluded_runs'])
                or not isinstance(data.get('records'), dict)
                or not isinstance(data.get('binding'), dict)):
            raise ValueError('invalid recovery state')
        for eid, row in data['records'].items():
            if (not re.fullmatch('[0-9a-f]{64}', eid) or not isinstance(row, dict)
                    or row.get('status') not in {'waiting', 'attempted', 'delivered',
                                                'acknowledged', 'expired', 'conflict'}
                    or not w._finite_time(row.get('due'))
                    or not w._finite_time(row.get('expires'))
                    or not isinstance(row.get('event'), dict)
                    or row['event'].get('id') != eid
                    or (row.get('reminder_comment') is not None and
                        (type(row['reminder_comment']) is not int or row['reminder_comment'] < 1))):
                raise ValueError('invalid recovery record')
        return data


class Recovery:
    def __init__(self, source, store, project, repo, pr, actor,
                 github_factory=w.GitHub, clock=time.time):
        # Reuse exact destination validation; constructor performs no network I/O.
        w.GitHub(repo, pr, actor)
        if not w.PROJECT.fullmatch(project):
            raise ValueError('invalid project')
        if source.directory.resolve() == store.directory.resolve():
            raise ValueError('recovery needs separate private state')
        self.source, self.store = source, store
        self.binding = dict(project=project, repo=repo, pr=pr, actor=actor)
        self.github_factory, self.clock = github_factory, clock

    def activate(self):
        with self.source.locked(), self.store.locked():
            if self.store.path.exists() or self.store.path.is_symlink():
                raise ValueError('already initialised; never reset recovery history')
            source = self.source.load()
            self.store.save(dict(version=1, enabled=True, binding=self.binding,
                                 excluded_runs=sorted(source['runs']), records={}))

    def _state(self):
        state = self.store.load()
        if state['binding'] != self.binding:
            raise ValueError('recovery route or actor changed')
        return state

    def set_enabled(self, enabled):
        with self.store.locked():
            state = self._state()
            state['enabled'] = bool(enabled)
            self.store.save(state)

    def _valid(self, event, runs):
        try:
            run = runs[event['run_id']]
            return (w.RUN_ID.fullmatch(event['run_id'])
                    and event['kind'] in w.KINDS
                    and type(event['generation']) is int and event['generation'] > 0
                    and type(event['comment_id']) is int and event['comment_id'] > 0
                    and event['id'] == w.event_id(event['project'], event['run_id'],
                                                event['kind'], event['generation'])
                    and all(event[k] == run[k] for k in ('project', 'repo', 'pr', 'correlation'))
                    and all(event[k] == self.binding[k] for k in ('project', 'repo', 'pr'))
                    and event['delivery'] == 'delivered')
        except (KeyError, TypeError):
            return False

    def tick(self):
        # Same source lock as observer: read a stable registry and do not race its writes.
        # At most one route GET and one POST (8 + 18 seconds) per invocation.
        # Wait only for the shared source lock. Never wait for the companion
        # lock while holding source: another companion invocation must fail
        # closed rather than extend source contention. No network/write retry.
        with self.source.locked(wait_seconds=w.TICK_LOCK_WAIT), self.store.locked():
            state, source = self._state(), self.source.load()
            if not state['enabled'] or not source['enabled']:
                return state
            now = self.clock()
            for eid, event in source['events'].items():
                if (event['run_id'] in state['excluded_runs'] or eid in state['records']
                        or event['ack'] == 'acknowledged' or not self._valid(event, source['runs'])):
                    continue
                state['records'][eid] = dict(event=dict(event), status='waiting',
                    due=now + GRACE, expires=now + LIFETIME, reminder_comment=None)
            self.store.save(state)
            rows = [r for r in state['records'].values()
                    if r['status'] in {'waiting', 'attempted', 'delivered'}]
            for row in rows:
                current = source['events'].get(row['event']['id'])
                if (current is not None and self._valid(current, source['runs'])
                        and all(current[k] == row['event'][k] for k in
                                ('id', 'run_id', 'project', 'repo', 'pr', 'correlation',
                                 'kind', 'generation', 'comment_id'))
                        and current['ack'] == 'acknowledged'):
                    row['status'] = 'acknowledged'
                elif now >= row['expires']:
                    row['status'] = 'expired'
            rows = [row for row in rows if row['status'] not in {'expired', 'acknowledged'}]
            self.store.save(state)
            if not rows:
                return state
            gh = self.github_factory(self.binding['repo'], self.binding['pr'], self.binding['actor'])
            try:
                comments = gh.comments()  # All pages; failed GET never permits POST.
            except ERRORS:
                return state
            posted = False
            for row in sorted(rows, key=lambda r: (r['due'], r['event']['id'])):
                event = row['event']
                current = source['events'].get(event['id'])
                if (current is None or not self._valid(current, source['runs'])
                        or any(current[k] != event[k] for k in
                               ('id', 'run_id', 'project', 'repo', 'pr', 'correlation',
                                'kind', 'generation', 'comment_id'))):
                    row['status'] = 'conflict'
                    continue
                actor_comments = [c for c in comments
                                  if c.get('user', {}).get('login') == self.binding['actor']]
                original = [c for c in actor_comments if c.get('id') == event['comment_id']
                            and c.get('body') == w.Watchdog._body(event)]
                if len(original) != 1:
                    row['status'] = 'conflict'
                    continue
                if any(c.get('body', '').strip() == w.ack_marker(event['id'], event['comment_id'])
                       for c in actor_comments):
                    row['status'] = 'acknowledged'
                    continue
                expected = body(event)
                marker = expected.split('\n', 1)[0]
                matches = [c for c in actor_comments if c.get('body', '').startswith(marker)]
                if matches:
                    if (len(matches) != 1 or matches[0].get('body') != expected
                            or type(matches[0].get('id')) is not int or matches[0]['id'] < 1):
                        row['status'] = 'conflict'
                    else:
                        row.update(status='delivered', reminder_comment=matches[0]['id'])
                        if now >= row['expires']:
                            row['status'] = 'expired'
                    continue
                if now >= row['expires']:
                    row['status'] = 'expired'
                elif row['status'] == 'waiting' and now >= row['due'] and not posted:
                    # Durable intent BEFORE POST. Even process death before the request
                    # sacrifices delivery rather than blindly replaying an uncertain write.
                    row['status'] = 'attempted'
                    self.store.save(state)
                    posted = True
                    try:
                        cid = gh.post(expected)
                        if type(cid) is int and cid > 0:
                            row.update(status='delivered', reminder_comment=cid)
                    except ERRORS:
                        pass  # Future ticks reconcile read-only, never resend.
            self.store.save(state)
            return state


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--watchdog-state', required=True)
    p.add_argument('--state-dir', required=True)
    for key in ('project', 'repo', 'actor'):
        p.add_argument('--' + key, required=True)
    p.add_argument('--pr', type=int, required=True)
    p.add_argument('command', choices=('activate', 'pause', 'resume', 'tick'))
    a = p.parse_args()
    recovery = Recovery(w.Store(a.watchdog_state), RecoveryStore(a.state_dir),
                        a.project, a.repo, a.pr, a.actor)
    if a.command == 'activate':
        recovery.activate()
    elif a.command in ('pause', 'resume'):
        recovery.set_enabled(a.command == 'resume')
    else:
        recovery.tick()


if __name__ == '__main__':
    main()
