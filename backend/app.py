"""AudioClip — single-user YouTube-to-MP3 service."""
import hmac
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
TOKEN = os.getenv('API_TOKEN', '')
if os.getenv('RENDER') == 'true' and not TOKEN:
    raise RuntimeError('Render 배포에는 API_TOKEN이 필요합니다.')
ORIGINS = os.getenv('ALLOWED_ORIGINS', 'http://localhost:8000,http://127.0.0.1:8000').split(',')
TTL = 3600
app = FastAPI(title='AudioClip', docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=['GET','POST'], allow_headers=['Authorization','Content-Type'])
lock = threading.Lock()
CONCURRENCY = max(1, min(2, int(os.getenv('MAX_CONCURRENT_JOBS', '2'))))
slots = threading.BoundedSemaphore(CONCURRENCY)
pool = ThreadPoolExecutor(max_workers=CONCURRENCY)
jobs = {}
cache = {}


def auth(request: Request):
    if TOKEN and not hmac.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + TOKEN):
        raise HTTPException(401, '서버 연결 키가 올바르지 않습니다.')


def canonical_url(raw: str) -> str:
    try:
        u = urlparse(raw.strip())
        if u.scheme not in ('https', 'http') or u.username or u.password or u.port not in (None, 80, 443):
            raise ValueError()
        host = (u.hostname or '').lower()
        if host == 'youtu.be':
            vid = u.path.strip('/')
        elif host in ('youtube.com','www.youtube.com','m.youtube.com','music.youtube.com'):
            if u.path == '/watch':
                vid = parse_qs(u.query).get('v',[''])[0]
            else:
                match = re.fullmatch(r'/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})/?', u.path)
                vid = match.group(1) if match else ''
        else:
            raise ValueError()
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', vid):
            raise ValueError()
        return 'https://www.youtube.com/watch?v=' + vid
    except (ValueError, TypeError):
        raise HTTPException(400, '올바른 유튜브 영상 주소를 입력해 주세요. 재생목록 주소 대신 영상 주소가 필요합니다.')


def run_cmd(args, timeout):
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        raise RuntimeError('처리 시간이 초과되었습니다. 더 짧은 구간으로 다시 시도해 주세요.')
    if p.returncode:
        # Log only a fixed category; never expose signed URLs or authentication data.
        lower = err.lower()
        if '429' in lower or 'too many requests' in lower:
            category = 'rate_limited'
            message = '유튜브 요청 횟수 제한(429)에 걸렸습니다. 반복 요청을 멈추고 시간을 두고 다시 시도해 주세요.'
        elif any(x in lower for x in ('not a bot', 'sign in', 'login required')):
            category = 'authentication_required'
            message = '유튜브가 로그인 또는 사람 확인을 요구했습니다. 이 서버는 해당 인증을 지원하지 않아 영상을 가져올 수 없습니다. 앱의 연결 키 문제가 아닙니다.'
        elif '403' in lower or 'forbidden' in lower:
            category = 'access_denied'
            message = '유튜브가 접근을 거부했습니다(403). 서버 네트워크 제한이나 유튜브의 요청 인증 조건 등이 원인일 수 있으며, 이 메시지만으로 구분할 수 없습니다.'
        elif any(x in lower for x in ('private video', 'video is unavailable', 'video unavailable', 'not available in your country')):
            category = 'video_unavailable'
            message = '비공개·삭제·지역 제한 등으로 이 서버에서 영상을 이용할 수 없습니다.'
        else:
            category = 'processing_failed'
            message = '영상을 처리하지 못했습니다. 공개 영상인지 확인해 주세요. 서버 로그의 downloader_error 항목으로 오류 유형을 확인할 수 있습니다.'
        print(f'downloader_error category={category} exit_code={p.returncode}', file=sys.stderr, flush=True)
        raise RuntimeError(message)
    return out


def base_cmd():
    return [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist', '--no-warnings', '--socket-timeout', '20', '--js-runtimes', 'node']


def info_for(url):
    now = time.time()
    with lock:
        hit = cache.get(url)
        if hit and now - hit[0] < 600:
            return hit[1]
    data = json.loads(run_cmd(base_cmd() + ['--skip-download', '--dump-single-json', '--', url], 60))
    duration = data.get('duration') or 0
    if data.get('is_live') or not math.isfinite(duration) or duration <= 0 or duration > 43200:
        raise RuntimeError('실시간 방송 및 12시간을 넘는 영상은 지원하지 않습니다.')
    info = {'id': data['id'], 'title': data.get('title','YouTube audio'), 'duration': duration,
            'channel': data.get('uploader','YouTube'),
            'chapters': [{'title': c.get('title','챕터'), 'start': c['start_time'], 'end': min(c['end_time'],duration)} for c in data.get('chapters') or []]}
    with lock:
        for key in list(cache):
            if now - cache[key][0] >= 600:
                del cache[key]
        if len(cache) >= 100:
            cache.pop(next(iter(cache)))
        cache[url] = (now, info)
    return info


def clean():
    now = time.time()
    with lock:
        expired = [key for key,j in jobs.items() if j['status'] in ('ready','error') and now-j.get('finished',j['created']) > TTL]
        for key in expired:
            shutil.rmtree(jobs.pop(key)['dir'], ignore_errors=True)


class VideoInput(BaseModel):
    url: str = Field(max_length=2048)


class ClipInput(VideoInput):
    start: float = Field(default=0, ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    bitrate: int = 192
    filename: str = Field(default='', max_length=100)


@app.get('/healthz')
def readiness():
    return {'ok': True}


@app.get('/api/health', dependencies=[Depends(auth)])
def health():
    import importlib.util
    return {'ok': True, 'ffmpeg': bool(shutil.which('ffmpeg')), 'yt_dlp': importlib.util.find_spec('yt_dlp') is not None, 'node': bool(shutil.which('node'))}


@app.post('/api/info', dependencies=[Depends(auth)])
def info(body: VideoInput):
    url = canonical_url(body.url)
    if not slots.acquire(blocking=False):
        raise HTTPException(429, '서버가 작업 중입니다. 잠시 후 다시 시도해 주세요.')
    try:
        return info_for(url)
    except (RuntimeError, ValueError, KeyError) as e:
        raise HTTPException(422, str(e) if isinstance(e, RuntimeError) else '영상 정보를 읽지 못했습니다.')
    finally:
        slots.release()


def convert(key, body, url, info):
    job = jobs[key]
    try:
        job['status'] = 'converting'
        run_cmd(base_cmd() + ['-f','bestaudio/best','--download-sections', f'*{body.start}-{body.end}',
            '--extract-audio','--audio-format','mp3','--audio-quality',f'{body.bitrate}K',
            '--max-filesize','1G','--no-progress','-o',str(Path(job['dir'])/'clip.%(ext)s'),'--',url], 1200)
        path = Path(job['dir'])/'clip.mp3'
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError('MP3 파일이 생성되지 않았습니다.')
        job.update(status='ready', size=path.stat().st_size)
    except Exception as e:
        job.update(status='error', error=str(e) if isinstance(e, RuntimeError) else '변환 중 오류가 발생했습니다.')
    finally:
        job['finished'] = time.time()
        slots.release()


@app.post('/api/jobs', dependencies=[Depends(auth)])
def create_job(body: ClipInput):
    clean()
    url = canonical_url(body.url)
    if body.bitrate not in (128,192,320):
        raise HTTPException(400, '지원하지 않는 음질입니다.')
    if body.end <= body.start or body.end - body.start > 14400:
        raise HTTPException(400, '종료 시간은 시작 시간보다 늦어야 하며, 한 번에 최대 4시간까지 가능합니다.')
    if not slots.acquire(blocking=False):
        raise HTTPException(429, '서버가 작업 중입니다. 잠시 후 다시 시도해 주세요.')
    try:
        info = info_for(url)
        if body.end > info['duration'] or body.start >= info['duration']:
            raise HTTPException(400, '영상 길이를 벗어난 구간입니다.')
        key = uuid.uuid4().hex
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', body.filename.strip() or info['title'])[:90].strip('. ') or 'audio'
        jobs[key] = {'status':'queued', 'created':time.time(), 'dir':tempfile.mkdtemp(prefix='audioclip-'), 'filename':name + '.mp3'}
        pool.submit(convert, key, body, url, info)
        return {'id':key}
    except RuntimeError as e:
        slots.release()
        raise HTTPException(422, str(e))
    except Exception:
        slots.release()
        raise


@app.get('/api/jobs/{key}', dependencies=[Depends(auth)])
def job_status(key: str):
    clean()
    job = jobs.get(key)
    if not job:
        raise HTTPException(404, '작업이 없거나 파일 보관 시간이 만료되었습니다.')
    return {k:v for k,v in job.items() if k not in ('dir', 'created', 'finished')}


@app.get('/api/jobs/{key}/file', dependencies=[Depends(auth)])
def download(key: str):
    clean()
    job = jobs.get(key)
    if not job or job['status'] != 'ready':
        raise HTTPException(404, '다운로드할 파일이 없습니다.')
    return FileResponse(Path(job['dir'])/'clip.mp3', media_type='audio/mpeg', filename=job['filename'])


def janitor():
    while True:
        time.sleep(60)
        clean()

threading.Thread(target=janitor, daemon=True).start()
app.mount('/', StaticFiles(directory=ROOT/'dist', html=True), name='web')
