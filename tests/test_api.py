import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from backend import app as module

INFO = {'id':'BaW_jenozKc','title':'Test audio','channel':'Test','duration':7200,'chapters':[]}
URL = 'https://www.youtube.com/watch?v=BaW_jenozKc'

class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(module.app)
        self.old_token = module.TOKEN
        module.TOKEN = ''

    def tearDown(self):
        module.TOKEN = self.old_token
        with module.lock:
            for key,j in list(module.jobs.items()):
                if j['status'] in ('ready','error'):
                    shutil.rmtree(j['dir'],ignore_errors=True)
                    del module.jobs[key]

    def test_url_canonicalization_and_ssrf_rejection(self):
        for url in ['https://youtu.be/BaW_jenozKc?t=20','https://youtube.com/shorts/BaW_jenozKc','https://music.youtube.com/watch?v=BaW_jenozKc&list=abc']:
            self.assertEqual(module.canonical_url(url),URL)
        for url in ['https://youtube.com.evil.test/watch?v=BaW_jenozKc','http://127.0.0.1/', 'file:///etc/passwd', 'https://youtube.com/playlist?list=abc','https://youtube.com:9000/watch?v=BaW_jenozKc', 'https://evil@youtube.com/watch?v=BaW_jenozKc']:
            with self.assertRaises(module.HTTPException):module.canonical_url(url)

    def test_token_authentication(self):
        module.TOKEN = 'test-only-key'
        self.assertEqual(self.client.get('/api/health').status_code,401)
        self.assertEqual(self.client.get('/api/health',headers={'Authorization':'Bearer test-only-key'}).status_code,200)

    def test_invalid_ranges_never_start_download(self):
        with patch.object(module,'info_for',return_value=INFO), patch.object(module.pool,'submit') as submit:
            for start,end in [(200,100),(0,7201),(7200,7300),(0,15000)]:
                r=self.client.post('/api/jobs',json={'url':URL,'start':start,'end':end})
                self.assertEqual(r.status_code,400)
            self.assertEqual(self.client.post('/api/jobs',json={'url':URL,'start':-1,'end':2}).status_code,422)
            self.assertEqual(self.client.post('/api/jobs',json={'url':URL,'start':0,'end':2,'bitrate':999}).status_code,400)
            submit.assert_not_called()

    def test_real_ffmpeg_conversion_job_and_download(self):
        # Replace only external YouTube I/O; run actual ffmpeg and test API/file behavior.
        seen=[]
        def fixture_command(args,timeout):
            seen.extend(args)
            span=args[args.index('--download-sections')+1][1:]
            start,end=map(float,span.split('-'))
            path=Path(args[args.index('-o')+1].replace('%(ext)s','mp3'))
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-t',str(end-start),'-b:a','192k',str(path)],check=True,capture_output=True)
            return ''
        with patch.object(module,'info_for',return_value=INFO),patch.object(module,'run_cmd',side_effect=fixture_command):
            r=self.client.post('/api/jobs',json={'url':URL,'start':2110,'end':2112,'filename':'my:/song'})
            self.assertEqual(r.status_code,200,r.text)
            key=r.json()['id']
            for _ in range(100):
                job=self.client.get('/api/jobs/'+key).json()
                if job['status'] in ('ready','error'):break
                time.sleep(.03)
            self.assertEqual(job['status'],'ready',job)
            self.assertEqual(job['filename'],'my__song.mp3')
            self.assertNotIn('dir',job)
            self.assertIn('*2110.0-2112.0',seen)
            file=self.client.get('/api/jobs/'+key+'/file')
            self.assertEqual(file.status_code,200)
            self.assertEqual(file.headers['content-type'],'audio/mpeg')
            with tempfile.NamedTemporaryFile(suffix='.mp3') as temp:
                temp.write(file.content);temp.flush()
                probe=subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','json',temp.name],text=True)
                self.assertAlmostEqual(float(json.loads(probe)['format']['duration']),2,delta=.1)

    def test_download_failure_is_visible_and_slot_released(self):
        with patch.object(module,'info_for',return_value=INFO),patch.object(module,'run_cmd',side_effect=RuntimeError('처리 실패')):
            r=self.client.post('/api/jobs',json={'url':URL,'start':0,'end':2})
            key=r.json()['id']
            for _ in range(100):
                j=self.client.get('/api/jobs/'+key).json()
                if j['status']=='error':break
                time.sleep(.01)
            self.assertEqual(j['error'],'처리 실패')
            self.assertEqual(self.client.get('/api/jobs/'+key+'/file').status_code,404)

    def test_expired_files_removed(self):
        directory=tempfile.mkdtemp()
        module.jobs['expired']={'status':'ready','created':0,'dir':directory}
        self.assertEqual(self.client.get('/api/jobs/expired').status_code,404)
        self.assertFalse(Path(directory).exists())

if __name__=='__main__':unittest.main()
