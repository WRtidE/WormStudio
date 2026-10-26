"""Synthetic review transactions; never write human evidence to real runs."""
import json,shutil,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import analyze_phenotypes as metrics
import manual_worm_annotation as manual
import urllib.request,urllib.error
from pipeline_progress import PipelineProgress
import extract_centerlines,quality_stage,worm_pipeline
from quality_fixture import make_fixture
from workspace_sessions import identity_links,review_identity,remove_identity_review,identity_frame


class IdentityReviewIntegrationTests(unittest.TestCase):
    def test_review_creates_new_run_reuses_geometry_and_undo_restores(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config,source,_=make_fixture(root);base=root/'outputs'/config['sample']
            config.update(measurement_mode='pixels',scale_um_per_px=None)
            source.update(measurement_mode='pixels',scale_um_per_px=None,model='SAM2 Hiera Large')
            (base/'pipeline-config.json').write_text(json.dumps(config),encoding='utf-8')
            (base/'sam2-tracking/summary.json').write_text(json.dumps(source),encoding='utf-8')
            (base/'sam2-tracking/component-cleanup.json').write_text('{}',encoding='utf-8')
            (root/'config').mkdir()
            for name in ('phenotype_config.json','motion_constraints.json'):shutil.copy2(metrics.ROOT/'config'/name,root/'config'/name)
            settings=json.loads((root/'config/phenotype_config.json').read_text(encoding='utf-8'))
            with patch.object(metrics,'ROOT',root),patch.object(extract_centerlines,'ROOT',root),patch.object(worm_pipeline,'ROOT',root),patch.object(metrics,'render'),patch.object(metrics,'write_report'):
                quality_stage.run(config['sample']);extract_centerlines.run(config['sample'],'niu')
                original=metrics.analyze(config['sample'],settings,'niu','centerlines-niu')
                edge=next(e for e in identity_links(root,config['sample'])['edges'] if e['reference_end_frame'] is not None and e['accepted'])
                oldfiles={str(p.relative_to(base)):p.read_bytes() for p in base.rglob('*') if p.is_file()}
                reviewed=review_identity(root,config['sample'],edge['id'],'unknown','synthetic-test-only','Synthetic uncertainty, not real identity evidence')
                self.assertNotEqual(reviewed['sample'],config['sample'])
                report=metrics.analyze(reviewed['sample'],settings,'niu','centerlines-niu')
                record=identity_links(root,reviewed['sample'])['edges']
                changed=next(e for e in record if e['id']==edge['id'])
                self.assertFalse(changed['accepted']);self.assertEqual(changed['decision'],'human_unknown')
                accepted=review_identity(root,reviewed['sample'],edge['id'],'same','synthetic-test-only','Synthetic construction identity only')
                metrics.analyze(accepted['sample'],settings,'niu','centerlines-niu')
                accepted_rows=json.loads((root/'outputs'/accepted['sample']/'phenotypes-centerlines-niu/per_frame_metrics.json').read_text(encoding='utf-8'))
                first=next(r for r in accepted_rows if r['frame']==edge['start_frame'] and r['worm_id']==edge['worm_id'])
                self.assertTrue(first['identity_human_confirmed']);self.assertIsNone(first['speed_px_s'])
                self.assertTrue(all(r['head_candidate_angle_deg'] is None for r in accepted_rows))
                review_id=accepted['identity_reviews'][0]['id']
                undone=remove_identity_review(root,accepted['sample'],review_id)
                restored=metrics.analyze(undone['sample'],settings,'niu','centerlines-niu')
                self.assertEqual(restored['worms'][0]['valid_frames'],original['worms'][0]['valid_frames'])
                np.testing.assert_array_equal(np.load(base/'centerlines-niu/worm-1/centerlines.npy'),np.load(root/'outputs'/undone['sample']/'centerlines-niu/worm-1/centerlines.npy'))
                self.assertEqual(oldfiles,{str(p.relative_to(base)):p.read_bytes() for p in base.rglob('*') if p.is_file()})
                self.assertGreater(len(identity_frame(root,config['sample'],1,0)),1000)
                for decision,reviewer,evidence in [('bad','a','b'),('same','','evidence'),('same','a','')]:
                    with self.assertRaises(ValueError):review_identity(root,config['sample'],edge['id'],decision,reviewer,evidence)
                with self.assertRaises(ValueError):remove_identity_review(root,config['sample'],'missing')
                with self.assertRaises(ValueError):identity_frame(root,config['sample'],1,999)
                with patch.object(manual,'ROOT',root):
                    progress=PipelineProgress();progress.config=config
                    progress.data.update(status='complete',sample=config['sample'])
                    progress.bind_server(manual.create_server(progress=progress))
                    url=progress.url.removesuffix('/progress')
                    try:
                        with urllib.request.urlopen(url+'/api/identity-links?run='+config['sample']) as response:
                            self.assertTrue(json.load(response)['edges'])
                        with urllib.request.urlopen(url+'/api/identity-frame?run='+config['sample']+'&worm=1&frame=0') as response:
                            self.assertEqual(response.headers.get_content_type(),'image/jpeg')
                        with self.assertRaises(urllib.error.HTTPError) as stale:
                            urllib.request.urlopen(url+'/api/identity-links?run=not-current')
                        self.assertEqual(stale.exception.code,409)
                        body=dict(sample=config['sample'],link_id=edge['id'],decision='unknown',reviewer='synthetic-http-test',evidence='Synthetic-only HTTP check')
                        request=urllib.request.Request(url+'/api/review-identity',json.dumps(body).encode(),headers={'Content-Type':'application/json'})
                        with self.assertRaises(urllib.error.HTTPError) as token:urllib.request.urlopen(request)
                        self.assertEqual(token.exception.code,403)
                        request.add_header('X-Annotation-Token',progress.token)
                        with urllib.request.urlopen(request) as response:self.assertTrue(json.load(response)['ok'])
                        self.assertTrue(progress.next_ready.is_set())
                        self.assertEqual(progress.next_settings['identity_reviews'][0]['decision'],'unknown')
                        # Automatic head inference is another derived run, never a human vote.
                        progress.next_ready.clear();progress.next_settings=None
                        progress.data.update(status='complete',sample=config['sample'])
                        auto=urllib.request.Request(url+'/api/auto-head',json.dumps(dict(sample=config['sample'],head_mode='taper_seed')).encode(),headers={'Content-Type':'application/json'})
                        with self.assertRaises(urllib.error.HTTPError) as token:urllib.request.urlopen(auto)
                        self.assertEqual(token.exception.code,403)
                        stale=urllib.request.Request(url+'/api/auto-head',json.dumps(dict(sample='not-current')).encode(),headers={'Content-Type':'application/json','X-Annotation-Token':progress.token})
                        with self.assertRaises(urllib.error.HTTPError):urllib.request.urlopen(stale)
                        auto.add_header('X-Annotation-Token',progress.token)
                        with urllib.request.urlopen(auto) as response:self.assertTrue(json.load(response)['ok'])
                        automated=progress.next_settings
                        self.assertNotEqual(automated['sample'],config['sample'])
                        self.assertEqual(automated['head_detection'],'taper_seed')
                        self.assertEqual(automated.get('head_annotations'),config.get('head_annotations'))
                        self.assertEqual(automated.get('identity_reviews'),config.get('identity_reviews'))
                        np.testing.assert_array_equal(np.load(base/'centerlines-niu/worm-1/centerlines.npy'),np.load(root/'outputs'/automated['sample']/'centerlines-niu/worm-1/centerlines.npy'))
                    finally:progress.close()

if __name__=='__main__':unittest.main()
