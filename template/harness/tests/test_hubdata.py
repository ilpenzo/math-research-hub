"""Tests for core/hubdata.py on a throwaway fixture hub (portable).

Checks against a real hub's catalogs live in test_hubdata_local.py when present.

Run: python3 harness/tests/test_hubdata.py   (needs Python 3.11+, the server interpreter)
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
import unittest

HARNESS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE = os.path.join(HARNESS, 'core', 'hubdata.py')


def _load():
    sys.path.insert(0, HARNESS)
    try:
        from core import hubdata
        if os.path.abspath(hubdata.__file__) == MODULE:
            return hubdata
    except ImportError:
        pass
    spec = importlib.util.spec_from_file_location('mathdesk_hubdata', MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hubdata = _load()


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as stream:
        stream.write(text)


def _record(key, **extra):
    entry = dict(id=key, kind='result', title=f'Title {key}', topics=['t'], status='reported',
                 evidence_level='imported', novelty='unassessed', statement=f'Statement of {key}.',
                 hypotheses=['h'], limitations='l', evidence=[], depends_on=[], supersedes=[],
                 subsumed_by=[], source_refs=[])
    entry.update(extra)
    return entry


class FixtureHubTest(unittest.TestCase):
    """A throwaway hub in a temporary directory, for the paths the real hub cannot exercise safely."""

    def setUp(self):
        import json
        import tempfile
        self.json = json
        self.tmp = tempfile.TemporaryDirectory(prefix='mathdesk-hubdata-')
        root = self.root = os.path.realpath(self.tmp.name)
        _write(os.path.join(root, 'hub.json'), json.dumps(dict(
            schema_version=2, roots={}, catalog_layout='federated', legacy_default_project='demo',
            infrastructure_routes=['START_HERE.md'],
            projects={'demo': dict(title='Demo', roots=[], state='active', id_prefix='',
                                   catalog_state='initialized', document_routes=['projects/demo'])})))
        self.records = os.path.join(root, 'projects/demo/catalog/records.json')
        _write(self.records, json.dumps([_record('R001'), _record('C001', kind='correction', supersedes=['R001'])]))
        _write(os.path.join(root, 'projects/demo/catalog/sources.json'), '[]')
        # Aids come after a large section here, so the prefix reader must read on.
        big = [{'id': f'x{i}', 'blob': 'y' * 2000} for i in range(1500)]
        _write(os.path.join(root, 'projects/demo/catalog/knowledge.json'), json.dumps(
            {'schema_version': 1, 'checkpoints': big, 'aids': [{'id': 'K001', 'kind': 'gap', 'title': 'Gap',
                                                              'payload': {'question': {'text': 'Q?'}}}]}, indent=2))
        _write(os.path.join(root, 'START_HERE.md'), '# Start\n')
        _write(os.path.join(root, 'projects/demo/investigations/2026-01-01-x/NOTE.md'), '```\n# not a title\n```\n# Real title\n')
        _write(os.path.join(root, '.hub/x.md'), 'secret')
        os.makedirs(os.path.join(root, '.hub/locks'))
        open(os.path.join(root, '.hub/locks/publication.lock'), 'w').close()
        os.symlink('/etc/hosts', os.path.join(root, 'escape.md'))
        os.symlink(os.path.join(root, '.hub/x.md'), os.path.join(root, 'internal.md'))
        self.hub = hubdata.Hub(root)

    def tearDown(self):
        self.tmp.cleanup()

    def _bump_records(self, entries):
        _write(self.records, self.json.dumps(entries))
        future = time.time() + 5
        os.utime(self.records, (future, future))
        self.hub._last_check = float('-inf')

    def test_fixture_basics(self):
        self.assertEqual(self.hub.known_ids(), ['C001', 'K001', 'R001'])
        r = self.hub.record('R001')
        self.assertEqual([c['id'] for c in r['corrections']], ['C001'])
        self.assertEqual(self.hub.record('K001')['category'], 'aid')
        note = self.hub.read_document('projects/demo/investigations/2026-01-01-x/NOTE.md')
        self.assertEqual(note['project'], 'demo')
        docs = {d['path']: d for d in self.hub.documents()}
        self.assertEqual(docs['projects/demo/investigations/2026-01-01-x/NOTE.md']['title'], 'Real title')
        self.assertEqual(docs['projects/demo/investigations/2026-01-01-x/NOTE.md']['kind'], 'note')
        self.assertNotIn('escape.md', docs)
        for path in ['escape.md', 'internal.md', '.hub/x.md']:
            self.assertIsNone(self.hub.read_document(path), path)

    def test_reload_and_busy_gate(self):
        import fcntl
        self.assertNotIn('R002', self.hub.known_ids())
        with open(os.path.join(self.root, '.hub/locks/publication.lock'), 'rb') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)     # simulate a publication
            self._bump_records([_record('R001'), _record('R002')])
            self.assertNotIn('R002', self.hub.known_ids())                # previous snapshot keeps serving
            self.assertTrue(any('busy' in w for w in self.hub.status()['warnings']))
            cold = hubdata.Hub(self.root)            # a cold start never reads past the gate (review finding)
            with self.assertRaises(hubdata.HubBusy):
                cold.known_ids()
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        self.assertIn('R002', cold.known_ids())     # and recovers once the publication ends
        self.hub._last_check = float('-inf')
        self.assertIn('R002', self.hub.known_ids())
        self.assertEqual(self.hub.record('R001')['corrections'], [])

    def test_bad_registry_keeps_previous_entries(self):
        self.assertIn('R001', self.hub.known_ids())
        _write(self.records, '[{"id": "R001",')
        future = time.time() + 5
        os.utime(self.records, (future, future))
        self.hub._last_check = float('-inf')
        self.assertIn('R001', self.hub.known_ids())
        self.assertTrue(any('Unreadable' in w for w in self.hub.status()['warnings']))

    def test_documents_refresh_in_background(self):
        self.hub.documents()
        _write(os.path.join(self.root, 'projects/demo/NEW.md'), '# New\n')
        self.hub._docs_at -= hubdata.DOCUMENT_TTL_SECONDS + 1
        self.hub.documents()                                               # stale answer, refresh started
        deadline = time.time() + 10
        while time.time() < deadline and not any(d['path'] == 'projects/demo/NEW.md' for d in self.hub.documents()):
            time.sleep(0.05)
        self.assertTrue(any(d['path'] == 'projects/demo/NEW.md' for d in self.hub.documents()))

    def test_aid_prefix_reader(self):
        text = self.json.dumps({'schema_version': 1, 'aids': [{'id': 'K001'}], 'checkpoints': [1] * 10})
        self.assertEqual(hubdata._parse_aids(text), [{'id': 'K001'}])
        with self.assertRaises(hubdata._NeedMore):
            hubdata._parse_aids(text[:30])
        self.assertEqual(hubdata._parse_aids('{"schema_version": 1}'), [])


if __name__ == '__main__':
    unittest.main(verbosity=1)
