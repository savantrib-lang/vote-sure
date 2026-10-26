"""Database and Flask integration tests; computer vision is explicitly mocked."""
import os,sys,tempfile,types,unittest,time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import database
class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old=database.DB_PATH
        database.DB_PATH=os.path.join(self.tmp.name,'test.db');database.init_db()
        self.e=database.get_active_election()['id'];self.v=database.add_voter('TEST-1','Test')[0]
        self.c=database.add_candidate(self.e,'Candidate','Group')
    def tearDown(self):
        database.DB_PATH=self.old;self.tmp.cleanup()
    def test_concurrent_duplicate_and_deletion(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(lambda _:database.cast_vote(self.v,self.c,self.e)[0],range(6)))
        self.assertEqual(sum(results),1)
        self.assertEqual(database.get_candidate_by_id(self.c)['votes_count'],1)
        self.assertFalse(database.delete_voter(self.v));self.assertFalse(database.delete_candidate(self.c))
    def test_closed_and_wrong_election(self):
        conn=database.get_db_connection()
        with conn:other=conn.execute("INSERT INTO elections(title,status) VALUES('Other','active')").lastrowid
        self.assertFalse(database.cast_vote(self.v,self.c,other)[0])
        with conn:conn.execute("UPDATE elections SET status='closed' WHERE id=?",(self.e,))
        self.assertFalse(database.cast_vote(self.v,self.c,self.e)[0]);conn.close()
    def test_per_election_reset(self):
        self.assertTrue(database.cast_vote(self.v,self.c,self.e)[0])
        conn=database.get_db_connection()
        with conn:other=conn.execute("INSERT INTO elections(title,status) VALUES('Other','active')").lastrowid
        conn.close();cand=database.add_candidate(other,'Other','Group')
        self.assertFalse(database.get_voter_by_id(self.v)['has_voted'])
        self.assertTrue(database.cast_vote(self.v,cand,other)[0]);database.reset_all_votes(self.e)
        self.assertTrue(database.get_voter_by_id(self.v)['has_voted'])
        self.assertEqual(database.get_candidate_by_id(cand)['votes_count'],1)
    def test_migration(self):
        database.init_db();self.assertIsNone(database.add_voter('test-1','Duplicate')[0])
        conn=database.get_db_connection()
        self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[]);conn.close()
    def test_flask_ballot(self):
        fake=types.ModuleType('face_engine');fake.reload_enrolled_voters=lambda:None
        with patch.dict(sys.modules,{'face_engine':fake,'cv2':types.ModuleType('cv2')}):
            sys.modules.pop('app',None)
            import app
            app.app.config.update(TESTING=True);client=app.app.test_client()
            self.assertEqual(client.get('/dashboard').status_code,302)
            self.assertEqual(client.post('/register',data={'role':'admin'}).status_code,403)
            client.post('/login',data={'username':'admin','password':'admin123'})
            for route in ['/dashboard','/voters','/voters/add','/candidates','/booth','/results','/audit']:
                self.assertEqual(client.get(route).status_code,200,route)
            payload={'candidate_id':self.c,'election_id':self.e}
            self.assertEqual(client.post('/api/cast_vote',json=payload).status_code,403)
            with client.session_transaction() as sess:
                sess['booth_verified_voter_id']=self.v;sess['booth_verified_at']=time.time()
            self.assertEqual(client.get('/booth/authorize_voter/'+str(self.v)).status_code,302)
            self.assertEqual(client.get('/ballot').status_code,200)
            self.assertEqual(client.post('/api/cast_vote',json={'candidate_id':'bad','election_id':self.e}).status_code,400)
            self.assertTrue(client.post('/api/cast_vote',json=payload).get_json()['success'])
            self.assertEqual(client.post('/api/cast_vote',json=payload).status_code,403)
if __name__=='__main__':unittest.main()
