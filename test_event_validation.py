"""Fictional offline current-format callback checks (no GitHub or runtime calls)."""
import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).parent / 'tools/event_validation.py'
spec = importlib.util.spec_from_file_location('event_validation', PATH)
assert spec and spec.loader
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)
PRODUCER_SPEC = importlib.util.spec_from_file_location('candidate_watchdog', Path(__file__).parent / 'watchdog.py')
assert PRODUCER_SPEC and PRODUCER_SPEC.loader
producer = importlib.util.module_from_spec(PRODUCER_SPEC)
PRODUCER_SPEC.loader.exec_module(producer)

PROJECT = 'fictional-project'
RUN = 'run_' + 'a' * 32
CORRELATION = 'fictional-correlation'


def event(kind, generation):
    eid = producer.event_id(PROJECT, RUN, kind, generation)
    body = producer.Watchdog._body({'id': eid, 'kind': kind, 'project': PROJECT,
                                     'run_id': RUN, 'correlation': CORRELATION})
    assert 'Generation:' not in body
    return body, eid


class EventValidationTests(unittest.TestCase):
    def check(self, body, bound=3, handled=(), expected=None):
        return validator.validate_event(body, PROJECT, RUN, CORRELATION, bound, handled, expected)

    def test_first_qualification_fixed_generation_one(self):
        body, eid = event('completion', 1)
        self.assertEqual(self.check(body, expected=1), ('completion', 1, eid))
        with self.assertRaisesRegex(ValueError, 'no unique'):
            self.check(event('completion', 2)[0], expected=1)

    def test_repeated_kind_second_generation_and_gap_out_of_order(self):
        second, eid2 = event('waiting-for-approval', 2)
        first, eid1 = event('waiting-for-approval', 1)
        self.assertEqual(self.check(second), ('waiting-for-approval', 2, eid2))
        self.assertEqual(self.check(first, handled={eid2}), ('waiting-for-approval', 1, eid1))
        with self.assertRaisesRegex(ValueError, 'already handled'):
            self.check(second, handled={eid2})
        # Generation 3 can arrive even if generation 2 never reaches GitHub.
        third, eid3 = event('connection-blocked', 3)
        self.assertEqual(self.check(third), ('connection-blocked', 3, eid3))

    def test_wrong_hash_and_bound_exceeded(self):
        body, _ = event('completion', 2)
        with self.assertRaisesRegex(ValueError, 'no unique'):
            self.check(body.replace('Hermes watchdog: completion', 'Hermes watchdog: failed'))
        with self.assertRaisesRegex(ValueError, 'no unique'):
            self.check(event('overdue', 4)[0], bound=3)

    def test_body_and_binding_refusal(self):
        body, _ = event('completion', 1)
        for variant in (body.replace('Project: fictional-project', 'Project: other'),
                        body.replace('Correlation: fictional-correlation', 'Correlation: other'),
                        body + '\nextra', body.replace('<!-- hermes-watchdog:event:', 'prefix<!-- hermes-watchdog:event:')):
            with self.subTest(variant=variant[:40]), self.assertRaises(ValueError):
                self.check(variant)
        with self.assertRaisesRegex(ValueError, 'bound'):
            self.check(body, bound=0)


if __name__ == '__main__':
    unittest.main()
