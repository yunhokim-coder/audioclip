'use strict';
const $ = id => document.getElementById(id);
let server = localStorage.getItem('audioclip-server') || location.origin;
let token = sessionStorage.getItem('audioclip-token') || '';
let video = null, currentUrl = '', busy = false, connected = false, full = false, downloadBlob = '';
const fields = ['startTime','endTime','startRange','endRange','previewStart','previewEnd'];
function parseTime(text) {
  const value = text.trim();
  if (!/^\d{1,5}(?::\d{1,2}){0,2}$/.test(value)) return NaN;
  const parts = value.split(':').map(Number);
  if (parts.length > 1 && parts.slice(1).some(n => n >= 60)) return NaN;
  return parts.reduce((n,p) => n*60+p, 0);
}
function formatTime(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  return [Math.floor(seconds/3600),Math.floor(seconds/60)%60,seconds%60].map(n=>String(n).padStart(2,'0')).join(':');
}
function shortTime(seconds) { const t=formatTime(seconds); return seconds<3600?t.slice(3):t; }
function getVideoId(raw) {
  try {
    const u = new URL(raw);
    if (!['https:','http:'].includes(u.protocol) || u.username || u.password) return null;
    const host = u.hostname.toLowerCase();
    let id = '';
    if (host==='youtu.be') id=u.pathname.slice(1);
    else if (['youtube.com','www.youtube.com','m.youtube.com','music.youtube.com'].includes(host)) {
      id = u.pathname==='/watch' ? u.searchParams.get('v') : u.pathname.match(/^\/(?:shorts|live|embed)\/([\w-]{11})\/?$/)?.[1];
    }
    return /^[\w-]{11}$/.test(id||'') ? id : null;
  } catch {return null;}
}
function notice(message, error=false) { $('notice').textContent=message; $('notice').classList.toggle('error',error); }
function setConnection(ok) {
  connected=ok; $('connectionDot').classList.toggle('connected',ok);
  $('connectionLabel').textContent=ok?'변환 서버 연결됨':'서버 연결 설정'; updateRange();
}
async function api(path, options={}) {
  if (!server) throw new Error('변환 서버를 먼저 연결해 주세요. 오른쪽 위 서버 연결 설정에서 안내를 확인할 수 있어요.');
  let response;
  try { response=await fetch(server+path,{...options,headers:{...(options.body?{'Content-Type':'application/json'}:{}),...(token?{'Authorization':'Bearer '+token}:{}),...options.headers},signal:AbortSignal.timeout(path==='/api/info'?75000:30000)}); }
  catch { throw new Error('변환 서버에 연결할 수 없습니다. 서버 실행 상태, HTTPS 주소와 ALLOWED_ORIGINS 설정을 확인해 주세요.'); }
  if (!response.ok) {
    let data; try {data=await response.json();} catch {}
    if(response.status===404)throw new Error('이 주소에는 아직 변환 서버가 연결되지 않았어요. 서버 배포 후 연결해 주세요.');
    throw new Error(typeof data?.detail==='string'?data.detail:'서버 요청에 실패했습니다. 입력값과 서버 연결을 확인해 주세요.');
  }
  return response;
}
async function checkConnection() {
  if (!server) return;
  try {
    const h=await (await api('/api/health')).json();
    const ok=h.ok&&h.ffmpeg&&h.yt_dlp&&h.node;
    setConnection(ok);
    if(ok) notice('서버가 준비됐어요. 유튜브 영상 링크를 입력해 주세요.');
    else notice('서버에 FFmpeg, yt-dlp 또는 Node.js가 없습니다. 서버 설치를 확인해 주세요.',true);
  } catch(e) {setConnection(false);notice(e.message,true);}
}
function openSettings() {
  $('serverUrl').value=server; $('serverToken').value=token; $('settingsMessage').textContent=''; $('settingsDialog').showModal();
}
$('settingsButton').onclick=openSettings; $('setupInline').onclick=openSettings;
$('closeSettings').onclick=()=>$('settingsDialog').close();
$('settingsForm').onsubmit=async e=>{
  e.preventDefault();
  let u;
  try {u=new URL($('serverUrl').value.trim()); if(!['http:','https:'].includes(u.protocol)||u.username||u.password||u.search||u.hash)throw new Error();}
  catch {$('settingsMessage').textContent='올바른 서버 주소를 입력해 주세요.';return;}
  if(location.protocol==='https:'&&u.protocol!=='https:') {
    $('settingsMessage').textContent='이 사이트에서는 HTTPS 서버를 연결하세요. 내 PC 서버는 http://localhost:8000에서 웹앱을 직접 열어 사용하세요.';return;
  }
  const oldServer=server,oldToken=token;
  server=u.href.replace(/\/$/,'');token=$('serverToken').value.trim();
  $('connectButton').disabled=true; $('settingsMessage').textContent='서버를 확인하는 중…';
  try {
    const h=await (await api('/api/health')).json();
    if(!h.ok||!h.ffmpeg||!h.yt_dlp||!h.node)throw new Error('서버에 FFmpeg, yt-dlp, Node.js가 모두 설치되어야 합니다.');
    localStorage.setItem('audioclip-server',server);sessionStorage.setItem('audioclip-token',token);
    setConnection(true); $('settingsDialog').close(); notice('변환 서버가 연결됐어요. 영상을 불러와 주세요.');
  } catch(e) {server=oldServer;token=oldToken;$('settingsMessage').textContent=e.message;}
  finally {$('connectButton').disabled=false;}
};
function embedAt(seconds=0,end) {
  if(!video)return;
  const frame=document.createElement('iframe');
  const query=new URLSearchParams({start:String(Math.floor(seconds)),rel:'0'});
  if(end!==undefined)query.set('end',String(Math.ceil(end)));
  frame.src='https://www.youtube-nocookie.com/embed/'+video.id+'?'+query;
  frame.title=video.title;frame.allow='accelerometer; autoplay; encrypted-media; gyroscope; picture-in-picture';frame.allowFullscreen=true;
  $('player').replaceChildren(frame);
}
$('loadForm').onsubmit=async e=>{
  e.preventDefault(); if(busy)return;
  const id=getVideoId($('videoUrl').value.trim());
  if(!id){notice('올바른 유튜브 영상 주소를 입력해 주세요. 재생목록 주소라면 개별 영상 링크를 사용하세요.',true);return;}
  if(!connected){notice('MP3 변환을 위해 먼저 변환 서버를 연결해 주세요.',true);openSettings();return;}
  $('loadButton').disabled=true;$('loadButton').textContent='확인 중…';notice('영상 길이와 챕터를 불러오고 있어요…');
  try {
    const url='https://www.youtube.com/watch?v='+id;
    const data=await (await api('/api/info',{method:'POST',body:JSON.stringify({url})})).json();
    video=data;currentUrl=url;full=false;
    $('emptyVideo').hidden=true;$('loadedVideo').hidden=false;$('videoArea').classList.remove('empty');
    $('videoTitle').textContent=data.title;$('videoChannel').textContent=data.channel;$('videoDuration').textContent=shortTime(data.duration);
    $('filename').value=data.title.slice(0,90);
    $('startTime').value=formatTime(0);$('endTime').value=formatTime(data.duration);
    $('startRange').max=String(Math.floor(data.duration));$('endRange').max=String(Math.floor(data.duration));
    $('startRange').value=0;$('endRange').value=Math.floor(data.duration);
    $('timelineMid').textContent=shortTime(data.duration/2);$('timelineEnd').textContent=shortTime(data.duration);
    $('chapterSelect').replaceChildren(new Option('챕터 선택',''));
    (data.chapters||[]).forEach((chapter,i)=>$('chapterSelect').add(new Option(shortTime(chapter.start)+' · '+chapter.title,String(i))));
    $('chapters').hidden=!(data.chapters||[]).length;
    setMode(false);embedAt();clearDownload();$('jobMessage').textContent='';notice('영상이 준비됐어요. 원하는 시작·종료 시간을 지정해 주세요.');
  } catch(e){notice(e.message,true);}
  finally {$('loadButton').disabled=false;$('loadButton').innerHTML='불러오기 <span>↗</span>';}
};
function range() {return {start:parseTime($('startTime').value),end:parseTime($('endTime').value)};}
function clearDownload() {
  if(downloadBlob)URL.revokeObjectURL(downloadBlob);downloadBlob='';$('downloadLink').hidden=true;$('convertButton').hidden=false;
}
function updateRange() {
  const {start,end}=range();
  const valid=video&&Number.isFinite(start)&&Number.isFinite(end)&&start>=0&&end>start&&end<=video.duration&&end-start<=14400;
  $('convertButton').disabled=!valid||!connected||busy;
  for(const id of fields)$(id).disabled=!video||busy||full;
  $('rangeMode').disabled=busy;$('fullMode').disabled=busy;$('chapterSelect').disabled=busy;
  $('filename').disabled=busy;$('quality').disabled=busy;$('loadButton').disabled=busy;$('videoUrl').disabled=busy;
  if(!video)return;
  $('rangeSummary').textContent=valid?shortTime(start)+' → '+shortTime(end):'시간을 확인해 주세요';
  $('lengthSummary').textContent=valid?shortTime(end-start):'—';
  $('sizeSummary').textContent=valid?'약 '+((end-start)*Number($('quality').value)*1000/8/1024/1024).toFixed(1)+' MB':'—';
  if(valid){
    const left=start/video.duration*100,right=end/video.duration*100;
    $('selection').style.left=left+'%';$('selection').style.right=(100-right)+'%';
    $('leftHandle').style.left='calc('+left+'% - '+(left?2:0)+'px)';$('rightHandle').style.right='calc('+(100-right)+'% - '+(right<100?2:0)+'px)';
    $('startRange').value=start;$('endRange').value=end;
  }
  $('timeHint').textContent=valid?'시간은 시:분:초 또는 분:초로 입력하세요. 최대 4시간까지 저장할 수 있어요.':'시작·종료 시간을 확인하세요. 영상 길이 내에서 최대 4시간까지 선택할 수 있어요.';
}
function setMode(isFull) {
  full=isFull;
  $('fullMode').classList.toggle('active',full);$('rangeMode').classList.toggle('active',!full);
  $('fullMode').setAttribute('aria-pressed',String(full));$('rangeMode').setAttribute('aria-pressed',String(!full));
  if(video&&full){$('startTime').value=formatTime(0);$('endTime').value=formatTime(video.duration);}
  clearDownload();updateRange();
}
$('rangeMode').onclick=()=>setMode(false);$('fullMode').onclick=()=>setMode(true);
for(const id of ['startTime','endTime','quality'])$(id).addEventListener('input',()=>{clearDownload();updateRange();});
for(const id of ['startTime','endTime'])$(id).addEventListener('blur',()=>{const t=parseTime($(id).value);if(Number.isFinite(t))$(id).value=formatTime(t);});
$('startRange').oninput=()=>{
  const end=parseTime($('endTime').value);$('startTime').value=formatTime(Math.min(Number($('startRange').value),end-1));clearDownload();updateRange();
};
$('endRange').oninput=()=>{
  const start=parseTime($('startTime').value);$('endTime').value=formatTime(Math.max(Number($('endRange').value),start+1));clearDownload();updateRange();
};
$('chapterSelect').onchange=()=>{
  if(!$('chapterSelect').value)return;
  const c=video.chapters[Number($('chapterSelect').value)];
  setMode(false);$('startTime').value=formatTime(c.start);$('endTime').value=formatTime(c.end);$('filename').value=c.title.slice(0,90);updateRange();embedAt(c.start,c.end);
};
$('previewStart').onclick=()=>{const {start,end}=range();if(Number.isFinite(start)&&Number.isFinite(end)&&end>start)embedAt(start,end);};
$('previewEnd').onclick=()=>{const {start,end}=range();if(Number.isFinite(start)&&Number.isFinite(end)&&end>start)embedAt(Math.max(start,end-5),end);};
$('openVideo').onclick=()=>window.open(currentUrl,'_blank','noopener');
$('convertButton').onclick=async()=>{
  const {start,end}=range();if(!video||busy||!connected)return;
  busy=true;clearDownload();updateRange();$('progress').hidden=false;$('jobMessage').textContent='변환을 요청하는 중…';
  try {
    const job=await (await api('/api/jobs',{method:'POST',body:JSON.stringify({url:currentUrl,start,end,bitrate:Number($('quality').value),filename:$('filename').value})})).json();
    $('jobMessage').textContent='선택한 구간을 변환하고 있습니다. 이 탭을 열어 두세요.';
    const deadline=Date.now()+22*60*1000;
    let transientErrors=0;
    while(true){
      await new Promise(resolve=>setTimeout(resolve,1800));
      if(Date.now()>deadline)throw new Error('작업 상태 확인 시간이 초과되었습니다. 서버 상태를 확인해 주세요.');
      let status;
      try {status=await (await api('/api/jobs/'+job.id)).json();transientErrors=0;}
      catch(e){if(++transientErrors>=3)throw e;continue;}
      if(status.status==='error')throw new Error(status.error);
      if(status.status==='ready'){
        $('jobMessage').textContent='MP3 파일을 가져오는 중…';
        const blob=await (await api('/api/jobs/'+job.id+'/file')).blob();
        downloadBlob=URL.createObjectURL(blob);$('downloadLink').href=downloadBlob;$('downloadLink').download=status.filename;
        $('downloadLink').hidden=false;$('convertButton').hidden=true;
        $('jobMessage').textContent='완성됐어요! 다운로드 버튼을 눌러 저장하세요.';break;
      }
    }
  }catch(e){$('jobMessage').textContent=e.message;}
  finally{busy=false;$('progress').hidden=true;updateRange();}
};
window.addEventListener('beforeunload',e=>{if(busy){e.preventDefault();e.returnValue='';}});
if(server)checkConnection();
