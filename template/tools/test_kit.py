"""Public installer/workflow integration on synthetic, isolated project data."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import catalog_store as cs
import catalog_validation as cv
import kit
from recall import sessions, index, retrieve, knowledge


class KitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'hub'; self.root.mkdir()
        source = Path(__file__).resolve().parents[1]
        shutil.copytree(source/'templates',self.root/'templates')
        shutil.copyfile(source/'START_HERE.md',self.root/'START_HERE.md')
        (self.root/'hub.json').write_text(json.dumps(dict(schema_version=1,projects={},owner='Fixture')))
        kit.add_project(self.root,argparse.Namespace(name='groups',title='Finite groups',prefix='FG',description='Study finite groups.',conventions='- All groups are finite.'))

    def test_empty_install_identity_archive_and_retrieval(self):
        cat=cs.load_catalog(self.root)
        self.assertEqual(cv.validate(cat,self.root),[])
        self.assertEqual(cat.cfg['legacy_default_project'],'groups')
        kit.register(self.root,argparse.Namespace(move=False,new_identity=False))
        self.assertEqual(sessions.workspace_identity(self.root)['state'],'current')
        old=Path(self.temp.name)/'earlier-notes'; old.mkdir(); (old/'lemma.txt').write_text('Synthetic finite-group note.')
        kit.add_archive(self.root,argparse.Namespace(path=str(old),project=['groups'],alias=None))
        self.assertIn('earlier-notes',cs.load_catalog(self.root).cfg['roots'])
        self.assertEqual(index.refresh(self.root,semantic=False)['semantic']['state'],'disabled')
        self.assertTrue(retrieve.search(self.root,'finite groups',project='groups',semantic=False)['hits'])

    def test_atomic_put_compare_and_swap_and_invalid_batch(self):
        note=self.root/'proof.md';note.write_text('Synthetic fixture only.')
        entry=dict(id='auto',project='groups',kind='conjecture',title='Fixture claim',topics=['fixture'],
                   status='reported',evidence_level='draft-proof',novelty='unassessed',statement='Synthetic claim only.',
                   hypotheses=['Fixture domain'],limitations='Not mathematics.',
                   evidence=[dict(root='hub',path='proof.md',locator='line 1',sha256='auto')],
                   depends_on=[],supersedes=[],subsumed_by=[],source_refs=[])
        path=self.root/'entry.json';path.write_text(json.dumps(entry))
        args=argparse.Namespace(file=str(path),project=None,expect=[],owner='test')
        kit.put(self.root,args);cat=cs.load_catalog(self.root);saved=cat.by_id()['FG-R001']
        self.assertEqual(cv.validate(cat,self.root),[])
        updated=dict(saved,title='Changed fixture');path.write_text(json.dumps(updated))
        with self.assertRaisesRegex(ValueError,'update requires'):kit.put(self.root,args)
        args.expect=['FG-R001='+kit.entry_hash(saved)];kit.put(self.root,args)
        with self.assertRaisesRegex(ValueError,'update requires'):kit.put(self.root,args)
        before=(self.root/'projects/groups/catalog/reservations.json').read_bytes()
        invalid=dict(entry,depends_on=['MISSING-R001']);path.write_text(json.dumps(invalid));args.expect=[]
        with self.assertRaises(ValueError):kit.put(self.root,args)
        self.assertEqual(before,(self.root/'projects/groups/catalog/reservations.json').read_bytes())

    def test_new_note_second_project_and_saved_context(self):
        kit.add_project(self.root,argparse.Namespace(name='analysis',title='Analysis',prefix='AN',description='',conventions=''))
        self.assertEqual(len(cs.load_catalog(self.root).cfg['projects']),2)
        args=argparse.Namespace(command='new-note',topic='Synthetic example',project='groups',owner='test')
        with contextlib.redirect_stdout(io.StringIO()):kit.dispatch(self.root,args)
        with self.assertRaisesRegex(ValueError,'already exists'):kit.dispatch(self.root,args)
        kit.register(self.root,argparse.Namespace(move=False,new_identity=False))
        spec=dict(project='groups',native_thread=None,context_key='fixture',user_direction='Inspect a finite group.',
                  selected_direction='Compare examples.',constraints=[],decisions=[],results=[dict(text='A case remains open.',status='unresolved')],
                  unresolved=['A missing implication.'],next_action='Ask for current direction.',references=[],locators=[],captured_through={})
        sessions.checkpoint(self.root,spec,'codex','test')
        packet=sessions.resume(self.root,'codex',project='groups',query='finite group')
        self.assertEqual(packet['state'],'recovered');self.assertFalse(packet['bound'])
        self.assertEqual(packet['selected']['results'][0]['status'],'unresolved')


if __name__ == '__main__':
    unittest.main()
