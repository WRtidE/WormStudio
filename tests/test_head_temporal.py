import unittest
import numpy as np
from head_temporal import stabilize, geometry_link


class TemporalHeadTests(unittest.TestCase):
    def fixture(self):
        p=np.tile(np.column_stack([np.linspace(0,100,101),np.zeros(101)]),(20,1,1))
        rows=[]
        for i in range(20):
            flip=bool(i%2);p[i]=p[i,::-1] if flip else p[i]
            valid=i!=11;local=0 if i<11 else 1
            rows.append(dict(head_endpoint=(local^int(flip)) if valid else None,
                head_segment=(0 if i<11 else 1) if valid else -1,
                head_input_flipped=flip,head_reason='relative_endpoint_bending' if valid else 'posture_unreliable',
                head_evidence_score=.8 if i<11 else .3,head_anchor_count=0,
                head_identity_confirmed=False,head_status='unconfirmed_relative_motion_candidate'))
        return rows,p,np.zeros(20,int)

    def test_short_gap_opposite_local_vote_keeps_same_physical_end(self):
        rows,p,ids=self.fixture();original=p.copy()
        stabilize(rows,p,ids,12.,'twardis')
        for i,r in enumerate(rows):
            if i==11:self.assertIsNone(r['head_endpoint']);continue
            self.assertEqual(r['head_endpoint'],i%2)
            self.assertFalse(r['head_identity_confirmed'])
        self.assertTrue(rows[12]['head_temporal_conflict'])
        self.assertEqual(rows[12]['head_local_endpoint'],1)
        self.assertEqual(rows[12]['head_temporal_seed_frame'],0)
        self.assertNotEqual(rows[10]['head_segment'],rows[12]['head_segment'])
        np.testing.assert_array_equal(p,original)

    def test_time_identity_and_large_position_jump_break_links(self):
        for kind in ('time','identity','transient_identity','jump'):
            rows,p,ids=self.fixture();fps=12.
            if kind=='time':fps=4.
            elif kind=='identity':ids[12:]=1
            elif kind=='transient_identity':ids[11]=1
            else:p[12:,:,1]+=100
            stabilize(rows,p,ids,fps,'twardis')
            self.assertEqual(rows[12]['head_endpoint'],1,kind)
            self.assertNotEqual(rows[10]['head_temporal_block'],rows[12]['head_temporal_block'],kind)

    def test_manual_and_explicit_conflict_are_not_overridden(self):
        for kind in ('manual','shape_motion_conflict','appearance_motion_conflict','manual_only'):
            rows,p,ids=self.fixture()
            for r in rows[12:]:
                if kind=='manual':r.update(head_anchor_count=1,head_identity_confirmed=True)
                elif kind!='manual_only':r.update(head_endpoint=None,head_reason=kind)
            before=[r['head_endpoint'] for r in rows]
            stabilize(rows,p,ids,12.,'manual_only' if kind=='manual_only' else 'twardis')
            self.assertEqual(before,[r['head_endpoint'] for r in rows])

    def test_geometry_checks_whole_body_and_abstains_without_seed(self):
        a=np.column_stack([np.linspace(0,100,101),np.zeros(101)])
        self.assertEqual(geometry_link(a,a[::-1])['flip'],1)
        b=a.copy();b[:,1]=80*np.sin(np.linspace(0,np.pi,101))
        self.assertIsNone(geometry_link(a,b))
        rows,p,ids=self.fixture()
        for r in rows:r.update(head_endpoint=None,head_evidence_score=0.)
        stabilize(rows,p,ids,12.,'twardis')
        self.assertTrue(all(r['head_endpoint'] is None for r in rows))


if __name__=='__main__':unittest.main()
