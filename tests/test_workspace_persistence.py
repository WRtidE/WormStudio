import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error

import workspace_library as library
from workspace_sessions import get_draft, save_draft
import manual_worm_annotation as manual
from pipeline_progress import PipelineProgress


class WorkspacePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='worm-library-')
        self.root=Path(self.temp.name)

    def tearDown(self): self.temp.cleanup()

    def test_upload_survives_restart_and_selection_is_persistent(self):
        item=library.upload(self.root,io.BytesIO(b'a,b\n1,2'),7,'data.csv','experiment/data.csv','signature')
        library.select(self.root,item['id'])
        state=json.loads((self.root/'config/workspace.local.json').read_text(encoding='utf-8'))
        self.assertEqual(state['selected'],item['id'])
        self.assertEqual(library.listing(self.root)['files'][0]['relativePath'],'experiment/data.csv')
        self.assertEqual(library.media(self.root,item['id']).read_bytes(),b'a,b\n1,2')
        self.assertTrue(item['cached'])

    def test_paths_remain_links_and_same_path_is_not_duplicated(self):
        path=self.root/'sample.mp4';path.write_bytes(b'video')
        first=library.register(self.root,path);second=library.register(self.root,path)
        self.assertEqual(first['id'],second['id'])
        self.assertFalse((self.root/'input_videos').exists())
        path.unlink()
        self.assertFalse(library.listing(self.root)['files'][0]['available'])
        self.assertIsNone(library.media(self.root,'../../secret'))
        with self.assertRaises(ValueError): library.select(self.root,'../../secret')

    def test_interrupted_upload_leaves_no_partial_file_or_index(self):
        with self.assertRaisesRegex(ValueError,'传输中断'):
            library.upload(self.root,io.BytesIO(b'x'),10,'a.mp4')
        self.assertEqual(library.listing(self.root)['files'],[])
        self.assertFalse(list((self.root/'input_videos/library').glob('*')))

    def test_upload_rejects_unsupported_files_and_confines_names(self):
        with self.assertRaises(ValueError): library.upload(self.root,io.BytesIO(b'x'),1,'page.html')
        item=library.upload(self.root,io.BytesIO(b'x'),1,'../../CON.txt')
        self.assertTrue(Path(item['path']).is_relative_to(self.root/'input_videos/library'))
        self.assertEqual(item['name'],'file-CON.txt')

    def test_corrupt_index_is_not_silently_overwritten(self):
        folder=self.root/'config';folder.mkdir();index=folder/'workspace.local.json';index.write_text('{broken')
        with self.assertRaises(ValueError): library.upload(self.root,io.BytesIO(b'x'),1,'a.txt')
        self.assertEqual(index.read_text(),'{broken')

    def test_draft_latest_is_atomic_bounded_and_frame_specific(self):
        annotation=SimpleNamespace(identity={'video':'v.mp4','frame':3},output=self.root/'outputs/run/manual-initialization')
        for value in range(5): save_draft(annotation,{'objects':[value]})
        self.assertEqual(get_draft(annotation)['body']['objects'],[4])
        self.assertEqual(len(list((self.root/'outputs/annotation-drafts').rglob('*.json'))),1)
        annotation.identity['frame']=4
        self.assertIsNone(get_draft(annotation)['body'])
        annotation.identity['frame']=3;save_draft(annotation,{'submitted':True})
        self.assertIsNone(get_draft(annotation)['body'])

    def test_http_upload_auth_range_and_restarted_server(self):
        with patch.object(manual,'ROOT',self.root):
            progress=PipelineProgress();progress.bind_server(manual.create_server(progress=progress))
            base=progress.url.removesuffix('/progress')
            def upload(token):
                request=urllib.request.Request(base+'/api/library-upload?name=test.csv',data=b'abcdef',headers={'X-Annotation-Token':token})
                return urllib.request.urlopen(request)
            try:
                with self.assertRaises(urllib.error.HTTPError) as error: upload('wrong')
                self.assertEqual(error.exception.code,403)
                with upload(progress.token) as response: item=json.load(response)
            finally: progress.close()

            progress=PipelineProgress();progress.bind_server(manual.create_server(progress=progress))
            base=progress.url.removesuffix('/progress')
            try:
                with urllib.request.urlopen(base+'/api/library') as response:
                    self.assertEqual(json.load(response)['files'][0]['id'],item['id'])
                request=urllib.request.Request(base+item['url'],headers={'Range':'bytes=1-3'})
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.status,206);self.assertEqual(response.read(),b'bcd')
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(base+'/api/library-media?id=../../private')
                self.assertEqual(error.exception.code,404)
            finally: progress.close()

    def test_draft_restores_after_cancel_and_new_server_session(self):
        import cv2
        import numpy as np
        video=self.root/'sample.mp4'
        writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*'mp4v'),12,(128,80))
        for _ in range(6): writer.write(np.zeros((80,128,3),np.uint8))
        writer.release()
        body=dict(objects=[],pixelMode=True,step=2,poly=[[5,5],[10,10]])
        with patch.object(manual,'ROOT',self.root):
            for cycle in range(2):
                progress=PipelineProgress();progress.data['status']='ready'
                progress.bind_server(manual.create_server(progress=progress))
                base=progress.url.removesuffix('/progress')
                def post(route,data,token):
                    request=urllib.request.Request(base+route,json.dumps(data).encode(),headers={'X-Annotation-Token':token})
                    try:
                        with urllib.request.urlopen(request) as response: return json.load(response)
                    except urllib.error.HTTPError as error:
                        raise AssertionError(f'cycle={cycle} {route}: {error.read().decode()}') from error
                try:
                    opened=post('/api/new-video',dict(video_path=str(video)),progress.token)
                    self.assertEqual(opened['file']['path'],str(video.resolve()))
                    with urllib.request.urlopen(base+'/state') as response: token=json.load(response)['token']
                    if cycle==0:
                        post('/api/draft',body,token)
                        post('/api/cancel',{},progress.token)
                        self.assertEqual(progress.snapshot()['status'],'cancelled')
                    else:
                        with urllib.request.urlopen(base+'/api/draft') as response:
                            self.assertEqual(json.load(response)['body'],body)
                        post('/api/annotation-frame',dict(frame=1),token)
                        with urllib.request.urlopen(base+'/api/draft') as response:
                            self.assertIsNone(json.load(response)['body'])
                finally: progress.close()



if __name__=='__main__': unittest.main()
