import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error

from app import workspace_library as library
from app import manual_worm_annotation as manual
from app.pipeline_progress import PipelineProgress
from app.workspace_sessions import catalog


class LibraryTreeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)

    def tearDown(self):self.temp.cleanup()

    def folder(self,name,parent=None):
        return library.organize(self.root,dict(op='mkdir',name=name,parent=parent))['folder']['id']

    def file(self,name='sample.csv',parent=None):
        return library.upload(self.root,io.BytesIO(b'a,b\n1,2'),7,name,parent=parent)

    def test_migrates_legacy_folder_paths_without_changing_ids_or_media(self):
        a=self.file('a.csv');b=self.file('b.csv')
        state=library.read(self.root);state.pop('version');state.pop('folders')
        for item,relative in zip(state['files'],['experiment/a.csv','experiment/nested/b.csv']):
            item.pop('parent');item.pop('order');item['relativePath']=relative
        library.write(self.root,state)
        result=library.listing(self.root);folders=result['folders']
        self.assertEqual({f['name'] for f in folders},{'experiment','nested'})
        self.assertEqual([f['id'] for f in result['files']],[a['id'],b['id']])
        self.assertEqual(library.listing(self.root),result)
        self.assertEqual(Path(a['path']).read_bytes(),b'a,b\n1,2')

    def test_new_folders_precede_loose_files_without_losing_manual_order(self):
        a=self.file('a.mp4');b=self.file('b.mp4')
        top=self.folder('实验');second=self.folder('另一组')
        def order(parent=None):return [n['id'] for n in library.nodes(library.read(self.root),parent)]
        self.assertEqual(order(),[top,second,a['id'],b['id']])
        library.organize(self.root,dict(op='move',id=b['id'],parent=None,before=a['id']))
        library.organize(self.root,dict(op='move',id=top,parent=None))
        self.assertEqual(order(),[second,b['id'],a['id'],top])
        new=self.folder('新实验')
        self.assertEqual(order(),[second,new,b['id'],a['id'],top])
        nested=self.file('nested.mp4',new)
        child=library.organize(self.root,dict(op='ensure-path',parts=['导入组'],parent=new))['folder']['id']
        self.assertEqual(order(new),[child,nested['id']])

    def test_nested_folder_import_sort_move_and_collapse_survive_reload(self):
        top=self.folder('实验');sub=self.folder('第一组',top)
        a=self.file('a.csv',sub);b=self.file('b.csv',sub)
        library.organize(self.root,dict(op='move',id=b['id'],parent=sub,before=a['id']))
        self.assertEqual([n['id'] for n in library.nodes(library.read(self.root),sub)],[b['id'],a['id']])
        library.organize(self.root,dict(op='move',id=b['id'],parent=top))
        library.organize(self.root,dict(op='collapse',id=top,collapsed=True))
        state=library.listing(self.root)
        self.assertEqual(next(f for f in state['files'] if f['id']==b['id'])['parent'],top)
        self.assertTrue(next(f for f in state['folders'] if f['id']==top)['collapsed'])
        self.assertEqual(Path(b['path']).read_bytes(),b'a,b\n1,2')

    def test_group_move_and_remove_validate_the_whole_selection_before_saving(self):
        folder=self.folder('复核')
        first=self.file('first.csv')
        second=self.file('second.csv',folder)
        last=self.file('last.csv',folder)
        ids=[second['id'],first['id']]
        library.select(self.root,first['id'])
        library.organize(self.root,dict(op='move',ids=ids,parent=folder,before=last['id']))
        self.assertEqual([f['id'] for f in library.nodes(library.read(self.root),folder)],ids+[last['id']])
        before=library.read(self.root)
        for op in ('move','remove'):
            with self.assertRaises(ValueError):
                library.organize(self.root,dict(op=op,ids=[first['id'],'missing']))
            self.assertEqual(library.read(self.root),before)
        result=library.organize(self.root,dict(op='remove',ids=ids))
        self.assertIsNone(result['selected'])
        self.assertEqual([f['id'] for f in result['files']],[last['id']])
        self.assertTrue(all(Path(f['path']).is_file() for f in (first,second)))

    def test_invalid_parent_cycle_name_and_sort_leave_saved_tree_unchanged(self):
        top=self.folder('实验');sub=self.folder('子文件夹',top);before=(self.root/'config/workspace.local.json').read_bytes()
        for body in [dict(op='move',id=top,parent=sub),dict(op='move',id=sub,parent='unknown'),
                     dict(op='move',id=sub,parent=None,before='unknown'),dict(op='mkdir',name='../unsafe'),
                     dict(op='mkdir',name='实验'),dict(op='ensure-path',parts=['valid','..'])]:
            with self.assertRaises(ValueError):library.organize(self.root,body)
            self.assertEqual((self.root/'config/workspace.local.json').read_bytes(),before)

    def test_remove_subtree_keeps_media_drafts_results_and_does_not_resurrect(self):
        top=self.folder('实验');sub=self.folder('子文件夹',top);a=self.file(parent=sub);other=self.file('other.csv')
        library.select(self.root,a['id'])
        run=self.root/'outputs'/'run';run.mkdir(parents=True)
        (run/'pipeline-config.json').write_text(json.dumps(dict(sample='run',video=a['path'])))
        (run/'draft.json').write_text('saved draft')
        before={p:p.read_bytes() for p in run.rglob('*') if p.is_file()}
        library.organize(self.root,dict(op='remove',id=top))
        state=library.listing(self.root)
        self.assertEqual([f['id'] for f in state['files']],[other['id']]);self.assertEqual(state['folders'],[])
        self.assertIsNone(state['selected']);self.assertIsNone(catalog(self.root)['runs'][0]['file'])
        self.assertEqual(Path(a['path']).read_bytes(),b'a,b\n1,2')
        for p,data in before.items():self.assertEqual(p.read_bytes(),data)
        restored=library.register(self.root,a['path'])
        self.assertEqual(restored['id'],a['id']);self.assertEqual(len(library.listing(self.root)['files']),2)

    def test_remove_folder_while_annotating_or_processing_keeps_session_and_media(self):
        with patch.object(manual, 'ROOT', self.root):
            for status in ('preparing', 'running'):
                with self.subTest(status=status):
                    folder = self.folder(status)
                    active = self.file('active.mp4', self.folder('nested', folder))
                    self.file('other.mp4', folder)
                    library.select(self.root, active['id'])
                    progress = PipelineProgress()
                    progress.config = dict(video=active['path'])
                    progress.data['status'] = status
                    progress.bind_server(manual.create_server(progress=progress))
                    try:
                        req = urllib.request.Request(progress.url.removesuffix('/progress')+'/api/library-tree',
                            data=json.dumps(dict(op='remove', id=folder)).encode(),
                            headers={'X-Annotation-Token':progress.token})
                        with urllib.request.urlopen(req) as response:
                            result = json.load(response)
                        self.assertEqual((result['files'], result['folders'], result['selected']), ([], [], None))
                        self.assertEqual(progress.snapshot()['status'], status)
                        self.assertEqual(Path(progress.config['video']).read_bytes(), b'a,b\n1,2')
                        self.assertEqual(library.media(self.root, active['id']), Path(active['path']))
                        self.assertFalse(progress.cancel_requested.is_set())
                    finally:
                        progress.close()

    def test_ensure_path_merges_only_same_parent_and_rename_only_changes_label(self):
        a=self.folder('A');b=self.folder('B')
        first=library.organize(self.root,dict(op='ensure-path',parts=['run','nested'],parent=a))['folder']['id']
        again=library.organize(self.root,dict(op='ensure-path',parts=['run','nested'],parent=a))['folder']['id']
        other=library.organize(self.root,dict(op='ensure-path',parts=['run','nested'],parent=b))['folder']['id']
        self.assertEqual(first,again);self.assertNotEqual(first,other)
        file=self.file(parent=first);library.organize(self.root,dict(op='rename',id=file['id'],name='新名称.csv'))
        changed=library.listing(self.root)['files'][0]
        self.assertEqual(changed['path'],file['path']);self.assertEqual(changed['name'],'新名称.csv')

    def test_upload_into_missing_folder_does_not_receive_or_store_media(self):
        with self.assertRaises(ValueError):self.file(parent='missing')
        self.assertFalse((self.root/'input_videos').exists())

    def test_http_token_and_new_server_preserve_tree(self):
        with patch.object(manual,'ROOT',self.root):
            p=PipelineProgress();p.data['status']='ready';p.bind_server(manual.create_server(progress=p))
            def post(body,token):
                return urllib.request.urlopen(urllib.request.Request(p.url.removesuffix('/progress')+'/api/library-tree',data=json.dumps(body).encode(),headers={'X-Annotation-Token':token}))
            try:
                with self.assertRaises(urllib.error.HTTPError) as error:post(dict(op='mkdir',name='test'),'wrong')
                self.assertEqual(error.exception.code,403)
                with post(dict(op='mkdir',name='test'),p.token) as response:folder=json.load(response)['folder']
                with post(dict(op='collapse',id=folder['id'],collapsed=True),p.token):pass
            finally:p.close()
            p=PipelineProgress();p.bind_server(manual.create_server(progress=p))
            try:
                with urllib.request.urlopen(p.url.removesuffix('/progress')+'/api/library') as response:
                    state=json.load(response)
                self.assertEqual(state['folders'][0]['id'],folder['id']);self.assertTrue(state['folders'][0]['collapsed'])
            finally:p.close()


if __name__=='__main__':unittest.main()
