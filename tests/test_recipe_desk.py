"""Visual editor boundaries and actual preview lifecycle on small synthetic models."""
import copy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import tomllib
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from recipe import compile_recipe
from recipe_desk import Desk, effective_document, handler_for, recipe_bytes
from test_recipe import fixture


class DeskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source.toml'
        self.original = fixture()
        self.raw = recipe_bytes(self.original)
        self.source.write_bytes(self.raw)
        self.desk = Desk(self.source, self.root/'draft.json', self.root/'previews')

    def tearDown(self):
        self.desk.close()
        self.tmp.cleanup()

    def ready(self, version=1):
        end = time.monotonic()+25
        while time.monotonic()<end:
            state = self.desk.snapshot()
            if state['preview_version']==version and state['status']=='ready':
                return state
            if state['status']=='error':
                self.fail(state['error'])
            time.sleep(.1)
        self.fail('preview did not finish: '+repr(self.desk.snapshot()))

    def test_round_trip_and_selection_do_not_reallocate_disabled_family(self):
        self.assertEqual(tomllib.loads(self.raw.decode()), self.original)
        document = self.desk.snapshot(True)['document']
        document['recipe']['families']['other']=9
        t=copy.deepcopy(document['recipe']['templates'][0]);t.update(id='other', family='other')
        document['recipe']['templates'].append(t)
        document['disabled_templates']=['other']
        document['disabled_options']={'separator':[0]}
        recipe,raw,_,_=effective_document(document,self.original)
        self.assertEqual(recipe['families'],{'one':1})
        self.assertEqual(recipe['slots']['separator']['options'],[{'value':'_','weight':1}])
        self.assertEqual(tomllib.loads(raw.decode()),recipe)
        compile_recipe(recipe)
        self.assertEqual(len(document['recipe']['templates']),2)

    def test_actual_preview_edit_error_recovery_and_reopen(self):
        first=self.ready()
        self.assertEqual(first['report']['windows'][0]['rows'][0]['text'],'"-ab-cd"')
        document=self.desk.snapshot(True)['document']
        document['recipe']['slots']['separator']['options'][1]['weight']=100
        version=self.desk.submit(document,1)
        second=self.ready(version)
        self.assertEqual(second['report']['windows'][0]['rows'][0]['text'],'"_ab_cd"')
        self.assertEqual(second['previous'],first['report'])
        document['recipe']['templates'][0]['case']='unsupported'
        version=self.desk.submit(document,version)
        end=time.monotonic()+15
        while self.desk.snapshot()['status']!='error' and time.monotonic()<end:
            time.sleep(.1)
        failed=self.desk.snapshot()
        self.assertEqual(failed['status'],'error')
        self.assertEqual(failed['report'],second['report'])
        self.assertEqual(failed['preview_version'],version-1)
        document['recipe']['templates'][0]['case']='literal'
        self.desk.submit(document,version)
        self.ready(version+1)
        self.assertEqual(self.source.read_bytes(),self.raw)
        self.desk.close()
        self.desk=Desk(self.source,self.root/'draft.json',self.root/'previews')
        self.assertEqual(self.desk.document,document)
        self.assertEqual(self.ready()['report']['windows'][0]['rows'][0]['text'],'"_ab_cd"')

    def test_pins_conflicting_edits_and_external_draft_change(self):
        doc=self.desk.snapshot(True)['document']
        doc['recipe']['history']={'resource':'untrusted'}
        with self.assertRaisesRegex(ValueError,'pinned'):
            self.desk.submit(doc,1)
        doc=self.desk.snapshot(True)['document']
        self.desk.submit(doc,1)
        with self.assertRaisesRegex(ValueError,'another tab'):
            self.desk.submit(doc,1)
        (self.root/'draft.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'outside'):
            self.desk.submit(doc,2)
        self.assertEqual(self.source.read_bytes(),self.raw)

    def test_rapid_edits_only_publish_latest(self):
        self.ready()
        doc=self.desk.snapshot(True)['document']
        # Large enough to keep the old worker busy until the next edit cancels it.
        doc['recipe']['slots']['a']={'alphabet':'abcdefghijklmnopqrstuvwxyz'}
        doc['recipe']['slots']['b']={'alphabet':'abcdefghijklmnopqrstuvwxyz'}
        doc['recipe']['lengths']={'short':{'8':1}}
        doc['recipe']['templates'][0]['case']='independent-pattern'
        self.desk.submit(doc,1)
        end=time.monotonic()+5
        while self.desk.snapshot()['status']!='building' and time.monotonic()<end:
            time.sleep(.05)
        clean=copy.deepcopy(self.desk.document)
        clean['recipe']=fixture()
        clean['recipe']['slots']['separator']['options'][0]['weight']=7
        self.desk.submit(clean,2)
        result=self.ready(3)
        self.assertEqual(result['preview_version'],3)
        self.assertEqual(len(list((self.root/'previews').glob('desk-*'))),2)
        self.assertFalse(list((self.root/'previews').glob('.desk-building-*')))

    def test_local_api_requires_token_origin_and_revision(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.desk,'test-token'))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}'
        def request(path,body=None,**headers):
            return urlopen(Request(url+path,data=json.dumps(body).encode() if body else None,
                                  headers={'Content-Type':'application/json',**headers}),timeout=5)
        try:
            with self.assertRaises(HTTPError) as denied:
                request('/api/state')
            self.assertEqual(denied.exception.code,403)
            with self.assertRaises(HTTPError):
                request('/api/state',**{'X-Desk-Token':'test-token','Origin':'https://example.com'})
            with self.assertRaises(HTTPError):
                request('/',Host='other.example')
            with request('/api/state',**{'X-Desk-Token':'test-token'}) as response:
                state=json.load(response)
            self.assertEqual(state['visual_views'],1)
            with request('/views.js') as response:
                self.assertIn(b'window.deskViews',response.read())
            self.ready()
            with request('/api/view',{'version':1,'kind':'autopsy','rank':'1'},**{'X-Desk-Token':'test-token'}) as response:
                self.assertEqual(json.load(response)['text'],'"-ab-cd"')
            with request('/api/view',{'version':1,'kind':'heatmap','template':'pair','budget':'1'},**{'X-Desk-Token':'test-token'}) as response:
                self.assertEqual(json.load(response)['cells'][0]['in_budget'],'1')
            with request('/api/draft',{'version':state['version'],'document':state['document']},**{'X-Desk-Token':'test-token'}) as response:
                self.assertEqual(json.load(response)['version'],2)
            with self.assertRaises(HTTPError):
                request('/api/draft',{'version':1,'document':state['document']},**{'X-Desk-Token':'test-token'})
            with self.assertRaises(HTTPError):
                request('/api/view',{'version':1,'kind':'autopsy','rank':'1'},**{'X-Desk-Token':'test-token'})
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__=='__main__':
    unittest.main()
