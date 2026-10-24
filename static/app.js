// Shared state, API client, authentication, and workspace bootstrap.

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const STATUS = {inbox:'新发现',to_read:'待读',reading:'正在读',read:'已读',later:'稍后',excluded:'已排除',watching:'跟踪中'};
const STATUS_OPTIONS = ['inbox','to_read','reading','read','later','excluded','watching'];
const SOURCE_NAME = {arxiv:'arXiv',openalex:'OpenAlex',crossref:'Crossref',europepmc:'Europe PMC',semanticscholar:'Semantic Scholar',dblp:'DBLP',openreview:'OpenReview',tavily:'Tavily 联网发现',deepseek_search:'DeepSeek Search',model_web:'AI 工具检索',manual:'手动添加',import:'导入'};
const PAGE_NAMES = {home:'今日',discover:'发现论文',library:'文献库',reading:'阅读台',deepread:'精读空间',prereading:'读前知识',automation:'自动追踪',agents:'研究 Agent',analytics:'数据分析',settings:'系统设置'};
const UI_STATE_KEY = 'researchdesk.ui-state.v1';
function readUiState(){try{const value=JSON.parse(localStorage.getItem(UI_STATE_KEY)||'{}');return value&&typeof value==='object'?value:{}}catch{return {};}}
const persistedUiState = readUiState();
const requestedView = new URLSearchParams(location.search).get('view');
let authMode = {enabled:false,authenticated:true,user:null,bootstrap_required:false,bootstrap_token_path:''};
let workspaces = [];
let workspaceMembersLoaded = false;
let workspaceMembersLoading = false, workspaceMembers = [];
let appState = null, papers = [], queries = [], runs = [], aiJobs = [], syncJobs = [], pdfIngestJobs = [], pdfFolders = [], pdfImportPollTimer = null, metadataDraftJob = null, metadataDraftTimer = null, analytics = null, recommendations = null, briefing = null, settingsSnapshot = null, activeView = PAGE_NAMES[requestedView]?requestedView:(PAGE_NAMES[persistedUiState.activeView]?persistedUiState.activeView:'home'), libraryStatus = '', libraryQuery = '', libraryTopic = '', librarySort = 'recent', libraryFavorite = false, libraryHasCard = false, libraryPage = {items:[],total:0,limit:50,offset:0,has_more:false,topics:[]}, libraryRequestId = 0, discoveryResults = [], discoveryMeta = null, discoveryQuery = '', activeDiscoveryRunId = persistedUiState.discoveryRunId||'', discoveryRestoreTimer = null, discoveryRestoreRunId = '', discoveryRestoreAttempts = 0, discoveryTriageTimer = null, discoveryTriageRunId = '', discoveryTriageAttempts = 0, discoveryTriageFilter = 'all', discoveryHistory = [], discoveryHistoryLoaded = false, discoveryHistoryLoading = null, discoveryHistoryFetchedAt = 0, discoveryHistoryError = '', selectedPaperId = persistedUiState.selectedPaperId||'', deepReadPaperId = persistedUiState.deepReadPaperId||'', deepReadState = {paper:null, analyses:[], jobs:[], loading:false, error:'', filter:persistedUiState.deepReadFilter||'overview', queueFilter:persistedUiState.deepReadQueueFilter||'all', queueQuery:persistedUiState.deepReadQueueQuery||'', segments:[], segmentation:{mode:'structure'}, segmentationSubmitting:false, pageNumbers:[], pdfPage:Number(persistedUiState.deepReadPdfPage||0), segmentIndex:Number(persistedUiState.deepReadSegmentIndex||0), segmentsLoading:false, segmentsError:'', paragraphJobId:'', queueTimer:null}, activeModalPaperId = '', agentConfigured = false, modelConfigState = {status:'loading',error:'',fetchedAt:0}, modelConfigTimer = null, modelConfigInFlight = null, selectedLibraryIds = new Set(), aiDrafts = {}, aiDeepReads = {}, aiAnalyses = {}, aiJobPollers = {}, globalAiJobsTimer = null, uiStateRestored = false, agentGoals = [], agentTools = [], activeAgentGoalId = localStorage.getItem('researchdesk.agent-goal')||'', activeAgentRun = null, agentMemory = {papers:[],critiques:[]}, agentWorkspaceTimer = null, agentModelConfigured = false, agentWorkspaceLoading = false;
let recommendationPollTimer = null, recommendationPollAttempts = 0;
let semanticIndexPollers = {};
const METADATA_DRAFT_JOB_KEY='litloom.metadata-draft-job.v1';
const DISCOVERY_DEFAULT_OPTIONS={expand_concepts:true,expand_relations:false,suppress_seen:false,fill_repeats:true,avoid_repeats:true,ai_screen:true,ai_expand:true,search_backend:'model_web',sort:'relevance',search_depth:'balanced',discovery_mode:'balanced'};
let discoveryDraft=persistedUiState.discoveryDraft||{query:'',sources:['arxiv','openalex'],options:{...DISCOVERY_DEFAULT_OPTIONS}}, discoveryDraftRunId=persistedUiState.discoveryDraftRunId||'', selectedCandidateIds=new Set(persistedUiState.selectedCandidateIds||[]), discoveryHistoryOffset=0, discoveryHistorySearch='', discoveryHistorySearchTimer=null;
let studyPage={items:[],total:0,limit:120,offset:0,has_more:false,counts:{new:0,learning:0,mastered:0},by_type:{prerequisite:0,glossary:0,notation:0,self_check:0},review:{due_count:0,overdue_count:0,due_only:false},paper_options:[]},studyQuery='',studyMastery='',studyType='',studyPaperId=persistedUiState.prereadingPaperId||'',studyDueOnly=false,studyRequestId=0,studySearchTimer=null;
if(!localStorage.getItem('litloom.model-search-default.v1')){discoveryDraft={...discoveryDraft,options:{...DISCOVERY_DEFAULT_OPTIONS,...(discoveryDraft.options||{}),search_backend:'model_web'}};localStorage.setItem('litloom.model-search-default.v1','1');persistUiState();}
discoveryTriageFilter=persistedUiState.discoveryTriageFilter||'recommended';
let discoveryLibraryFilter=persistedUiState.discoveryLibraryFilter||'new';
function persistUiState(){try{localStorage.setItem(UI_STATE_KEY,JSON.stringify({activeView,selectedPaperId,deepReadPaperId,deepReadFilter:deepReadState.filter||'overview',deepReadQueueFilter:deepReadState.queueFilter||'all',deepReadQueueQuery:deepReadState.queueQuery||'',deepReadSegmentIndex:Number(deepReadState.segmentIndex||0),deepReadPdfPage:Number(deepReadState.pdfPage||0),prereadingPaperId:studyPaperId,openPaperId:activeModalPaperId,discoveryRunId:activeDiscoveryRunId,discoveryDraft,discoveryDraftRunId,discoveryTriageFilter,discoveryLibraryFilter,selectedCandidateIds:[...selectedCandidateIds],updatedAt:new Date().toISOString()}));}catch{}}
function discoveryStatusActive(status=discoveryMeta?.status){return ['queued','running'].includes(String(status||''));}
function discoveryStatusLabel(status){return ({queued:'排队中',running:'检索中',success:'已完成',partial:'部分完成',failed:'失败',interrupted:'已中断',cancelled:'已取消'})[status]||status||'未知状态';}
function discoveryDisplayStatus(meta={}){
  const status=String(meta.status||'');
  // Candidate results are durable even if a later persistence step failed.
  // Keep the diagnostic visible without making a usable result look lost.
  if(status==='failed'&&((Number(meta.accepted_count)||0)>0||(Number(meta.result_count)||0)>0||(meta.results_available&&Number(meta.candidate_count||0)>0)))return 'partial';
  return status;
}
function discoveryProgressText(meta=discoveryMeta){
  if(!meta)return '';
  const sourceTotal=Number(meta.source_total||meta.progress_total||0), sourceIndex=Number(meta.source_index||meta.progress_current||0);
  const source=meta.current_source?` · ${SOURCE_NAME[meta.current_source]||meta.current_source}`:'';
  const stage=discoveryStageLabel(meta.stage);
  return `${discoveryStatusLabel(meta.status)}${stage?` · ${stage}`:''}${sourceTotal?` · 来源 ${Math.min(sourceIndex,sourceTotal)}/${sourceTotal}`:''}${source}`;
}
function discoveryStageLabel(stage=''){
  if(String(stage).startsWith('model_web_search:'))return String(stage).slice('model_web_search:'.length);
  if(stage==='planning')return '正在规划检索';
  if(stage==='semantic_planning')return '正在理解研究问题并扩展术语';
  if(stage==='model_web_search')return 'AI 正在规划并调用论文检索工具';
  if(stage==='fetching_parallel')return '并行检索来源';
  if(String(stage).startsWith('source:'))return `正在检索 ${SOURCE_NAME[String(stage).slice(7)]||String(stage).slice(7)}`;
  return stage||'';
}
function scheduleGlobalAiJobsRefresh(){
  if(globalAiJobsTimer)clearTimeout(globalAiJobsTimer);
  if(!aiJobs.some(job=>['queued','running'].includes(job.status))&&!syncJobs.some(job=>['queued','running'].includes(job.status))&&!['queued','running'].includes(metadataDraftJob?.status))return;
  globalAiJobsTimer=setTimeout(async()=>{try{const requests=[api('/api/ai-jobs?limit=30'),api('/api/sync-jobs?limit=20')];const metadataId=localStorage.getItem(METADATA_DRAFT_JOB_KEY);if(metadataId)requests.push(api(`/api/metadata-draft-jobs/${encodeURIComponent(metadataId)}`));const [aiData,syncData,metadataData]=await Promise.all(requests);aiJobs=aiData.jobs||[];syncJobs=syncData||[];if(metadataData)metadataDraftJob=metadataData;renderGlobalTaskStatus();if(settingsSnapshot){settingsSnapshot.aiJobs=aiJobs;settingsSnapshot.syncJobs=syncJobs;settingsSnapshot.metadataDraftJob=metadataDraftJob;}if(activeView==='home'||activeView==='settings')renderView();}catch{}scheduleGlobalAiJobsRefresh();},2000);
}
function applyModelConfiguration(settings){
  const value=settings&&typeof settings==='object'?settings:{};
  agentConfigured=Boolean(value.api_key_set&&value.enabled);
  modelConfigState={status:'ready',error:'',fetchedAt:Date.now(),...value};
  if(settingsSnapshot)settingsSnapshot.model=value;
  updateModelDependentControls();
}
function modelConfigurationHint(){
  if(modelConfigState.status==='loading')return '正在读取模型配置…';
  if(modelConfigState.status==='error')return '模型配置暂时无法读取，系统会自动重试';
  return agentConfigured?'可按需要运行':'请在系统设置配置模型';
}
function updateModelDependentControls(){
  const configured=Boolean(agentConfigured);
  $$('[data-model-required]').forEach(button=>{
    const sourceReady=button.dataset.sourceReady!=='false';
    const running=button.dataset.running==='true';
    const allowed=configured&&sourceReady&&!running;
    button.disabled=!allowed;
    if(!allowed){
      button.title=running?'任务正在运行':!configured?modelConfigurationHint():'请先添加或下载本地 PDF';
    }else if(button.dataset.modelTitle)button.title=button.dataset.modelTitle;
  });
  const status=$('#model-runtime-status');
  if(status)status.textContent=agentConfigured?'模型已就绪':modelConfigurationHint();
  const label=$('#model-runtime-label');
  if(label)label.textContent=agentConfigured?'模型已配置':modelConfigurationHint();
  $$('.full-ai-analysis [data-model-hint]').forEach(item=>{
    if(item.dataset.sourceReady==='false')item.textContent='先添加或下载本地 PDF';
    else item.textContent=agentConfigured?'可按需要单独运行':modelConfigurationHint();
  });
  $$('.deepread-actions [data-model-hint]').forEach(item=>{item.textContent=agentConfigured?'模型已就绪，可按阶段运行':modelConfigurationHint();});
}
function scheduleModelConfigurationRefresh(){
  if(modelConfigTimer)clearTimeout(modelConfigTimer);
  const delay=modelConfigState.status==='ready'?60000:4000;
  modelConfigTimer=setTimeout(()=>{loadModelConfiguration({render:false});},delay);
}
async function loadModelConfiguration({force=false}={}){
  if(modelConfigInFlight&&!force)return modelConfigInFlight;
  modelConfigInFlight=(async()=>{
    try{applyModelConfiguration(await api('/api/settings/model',{timeout_ms:5000}));}
    catch(error){agentConfigured=false;modelConfigState={...modelConfigState,status:'error',error:error.message||'模型配置读取失败',fetchedAt:Date.now()};updateModelDependentControls();}
    finally{modelConfigInFlight=null;scheduleModelConfigurationRefresh();}
  })();
  return modelConfigInFlight;
}
function restoreMetadataDraftJob(){
 const id=localStorage.getItem(METADATA_DRAFT_JOB_KEY);if(!id)return;
 api(`/api/metadata-draft-jobs/${encodeURIComponent(id)}`).then(job=>{metadataDraftJob=job;renderGlobalTaskStatus();if(job.status==='success'||job.status==='partial')applyMetadataDraftJobResult(job);scheduleGlobalAiJobsRefresh();}).catch(()=>{localStorage.removeItem(METADATA_DRAFT_JOB_KEY);metadataDraftJob=null;});
}
function watchMetadataDraftJob(jobId){
 if(metadataDraftTimer)clearTimeout(metadataDraftTimer);
 const tick=async()=>{try{const job=await api(`/api/metadata-draft-jobs/${encodeURIComponent(jobId)}`);metadataDraftJob=job;renderGlobalTaskStatus();if($('#add-paper-form'))renderMetadataDraftJob(job);if(['queued','running'].includes(job.status)){metadataDraftTimer=setTimeout(tick,1200);return;}if(job.status==='success'||job.status==='partial')applyMetadataDraftJobResult(job);scheduleGlobalAiJobsRefresh();}catch(error){const status=$('#paper-metadata-status');if(status)status.textContent=`任务状态暂时无法读取，后台仍会继续：${error.message}`;metadataDraftTimer=setTimeout(tick,4000);}};
 tick();
}
function applyMetadataDraftJobResult(job){
 const result=job?.result;if(!result)return;
 const form=$('#add-paper-form');if(form){for(const [key,value] of Object.entries(result.fields||{})){const field=form.elements.namedItem(key);if(field&&!String(field.value||'').trim()&&value!==null&&value!=='')field.value=Array.isArray(value)?value.join(', '):String(value);}const status=$('#paper-metadata-status');if(status){status.textContent=`已尝试用 ${result.source} 补全空字段。${result.warning||''}`;if(job.status==='partial'){const retry=document.createElement('button');retry.type='button';retry.className='text-link';retry.dataset.retryMetadataDraft=job.id;retry.textContent='重试 AI 识别';status.append(' ',retry);}}}
}
function renderMetadataDraftJob(job){
 const status=$('#paper-metadata-status');if(!status||!job)return;
 const retry=['failed','partial','interrupted'].includes(job.status)?` <button type="button" class="text-link" data-retry-metadata-draft="${esc(job.id)}">重试</button>`:'';
 const total=Number(job.progress_total||0),current=Math.min(total,Number(job.progress_current||0)),progress=total?` · ${current}/${total} · ${Math.round(current/total*100)}%`:'';
 const state=({queued:'排队中',running:'处理中',success:'完成',partial:'部分完成',failed:'失败',interrupted:'上次服务退出时中断'})[job.status]||job.status;
 status.innerHTML=`${esc(state)}${esc(progress)} · ${esc(job.message||'')}${job.error?` · ${esc(job.error)}`:''}${retry}`;
}
function updateGlobalAiJob(job){
  const index=aiJobs.findIndex(item=>item.id===job.id);
  if(index>=0)aiJobs[index]={...aiJobs[index],...job};else aiJobs.unshift(job);
  aiJobs=aiJobs.slice(0,30);
  renderGlobalTaskStatus();
  scheduleGlobalAiJobsRefresh();
}

function esc(s='') { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function fmtDate(value, time=false) { if(!value) return '尚未运行'; const d=new Date(value); if(Number.isNaN(d.getTime())) return value; return new Intl.DateTimeFormat('zh-CN',{month:'short',day:'numeric',...(time?{hour:'2-digit',minute:'2-digit'}:{})}).format(d); }
function timeAgo(value){if(!value)return '尚未运行';let m=Math.max(0,Math.floor((Date.now()-new Date(value).getTime())/60000));if(m<1)return '刚刚';if(m<60)return `${m} 分钟前`;let h=Math.floor(m/60);if(h<24)return `${h} 小时前`;return `${Math.floor(h/24)} 天前`;}
function getTags(p){return Array.isArray(p.tags)?p.tags:[];}
function toast(message, type='') { const el=document.createElement('div');el.className='toast '+type;el.textContent=message;$('#toast-root').append(el);setTimeout(()=>el.remove(),3600); }
const API_TIMEOUT_MS = 30000;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function cookieValue(name){const prefix=`${name}=`;return document.cookie.split(';').map(value=>value.trim()).find(value=>value.startsWith(prefix))?.slice(prefix.length)||'';}
async function api(url, options={}) {
  const method=(options.method||'GET').toUpperCase();
  const maxAttempts=2;
  const timeoutMs=Number(options.timeout_ms)||API_TIMEOUT_MS;
  let authRecoveryAttempted=false;
  let lastError;
  for(let attempt=0;attempt<maxAttempts;attempt++){
    const controller=new AbortController();
    const timer=setTimeout(()=>controller.abort(),timeoutMs);
    const externalSignal=options.signal;
    const abortExternal=()=>controller.abort();
    if(externalSignal){if(externalSignal.aborted)controller.abort();else externalSignal.addEventListener('abort',abortExternal,{once:true});}
    try{
      const headers={...(options.body instanceof FormData?{}:{'Content-Type':'application/json'}),...(options.headers||{})};
      if(authMode.enabled&&!['GET','HEAD','OPTIONS'].includes(method)&&!['/api/auth/login','/api/auth/bootstrap'].includes(new URL(url,location.href).pathname)){
        const csrf=cookieValue('litloom_csrf');if(csrf)headers['X-LitLoom-CSRF']=csrf;
      }
      const requestOptions={...options};delete requestOptions.timeout_ms;
      // Keep browser sessions and the CSRF cookie attached even when the
      // workspace is opened from a restored tab or after a service restart.
      const res=await fetch(url,{...requestOptions,credentials:'same-origin',signal:controller.signal,headers});
      let data;if((res.headers.get('content-type')||'').includes('application/json'))data=await res.json();else data=await res.text();
      if(!res.ok){
        if(res.status===401&&authMode.enabled&&!['/api/auth/login','/api/auth/bootstrap'].includes(new URL(url,location.href).pathname)){authMode.authenticated=false;renderAuthGate();}
        const detail=(data&&typeof data==='object'&&data.error)||`请求失败（${res.status}）`;
        const path=new URL(url,location.href).pathname;
        const staleCsrf=res.status===403&&authMode.enabled&&!authRecoveryAttempted&&
          path!=='/api/auth/local-login'&&/安全令牌|csrf/i.test(String(detail))&&
          Boolean(authMode.local_auto_login_available)&&authMode.user?.username==='local.default';
        if(staleCsrf){
          authRecoveryAttempted=true;
          try{
            await fetch('/api/auth/local-login',{method:'POST',credentials:'same-origin',
              headers:{'Content-Type':'application/json'},body:'{}'});
            continue;
          }catch{}
        }
        const retryable=method==='GET'&&[502,503,504].includes(res.status)&&attempt<maxAttempts-1;
        if(retryable){lastError=new Error(detail);await sleep(350);continue;}
        const requestError=new Error(detail);
        requestError.status=res.status;
        requestError.data=data;
        throw requestError;
      }
      return data;
    }catch(error){
      const timedOut=error?.name==='AbortError';
      lastError=timedOut?new Error(`请求超时（${Math.round(timeoutMs/1000)} 秒）：${url}`):error;
      const retryable=method==='GET'&&attempt<maxAttempts-1&&(!externalSignal||!externalSignal.aborted)&&(!timedOut)&&!Number.isInteger(error?.status);
      if(retryable){await sleep(350);continue;}
      throw lastError;
    }finally{
      clearTimeout(timer);
      externalSignal?.removeEventListener('abort',abortExternal);
    }
  }
  throw lastError||new Error('请求失败');
}
const jsonBody = value => JSON.stringify(value);
function renderAuthGate(message=''){
  const root=$('#auth-root');if(!root)return;
  const bootstrap=Boolean(authMode.bootstrap_required),path=authMode.bootstrap_token_path||'';
  root.innerHTML=`<main class="auth-screen"><form id="auth-form" class="auth-panel" autocomplete="on"><img src="/static/litloom-icon.png" alt="" class="auth-mark"><p class="eyebrow">LitLoom · 团队工作区</p><h1>${bootstrap?'创建工作区管理员':'登录研究空间'}</h1><p class="auth-copy">${bootstrap?'首次启用团队模式，请使用本机生成的一次性令牌创建管理员。':'登录后访问团队共享文献库与研究任务。'}</p>${bootstrap?`<label>一次性初始化令牌<input class="field-input" name="bootstrap_token" required autocomplete="off"></label><small class="auth-hint">令牌文件：<code>${esc(path)}</code></small><label>显示名称<input class="field-input" name="display_name" maxlength="120" placeholder="例如：林同学"></label>`:''}<label>用户名<input class="field-input" name="username" required minlength="3" maxlength="40" autocomplete="username" pattern="[A-Za-z0-9][A-Za-z0-9._-]{2,39}"></label><label>密码<input class="field-input" name="password" type="password" required minlength="${bootstrap?'12':'1'}" maxlength="256" autocomplete="${bootstrap?'new-password':'current-password'}" ${bootstrap?'':'autofocus'}></label>${bootstrap?'<small class="auth-hint">至少 12 位，并同时包含字母与数字。</small>':''}<div id="auth-error" class="auth-error" role="alert">${esc(message)}</div><button class="button primary auth-submit" type="submit">${bootstrap?'创建管理员并进入':'登录'}</button>${authMode.local_auto_login_available?'<button class="button small local-entry-button" type="button" data-local-login>默认用户进入</button>':''}<button class="text-link auth-retry" type="button" data-auth-retry>重新连接</button></form></main>`;
  root.hidden=false;
}
function renderAuthAccount(){
  const button=$('#auth-account-button'),profile=$('.profile-row'),switcher=$('#workspace-switcher');
  if(!button)return;
  button.hidden=!(authMode.enabled&&authMode.authenticated);
  if(authMode.enabled&&authMode.user){button.title=`${authMode.user.display_name||authMode.user.username} · ${authMode.user.role}`;const name=profile?.querySelector('b'),sub=profile?.querySelector('small');if(name)name.textContent=authMode.user.display_name||authMode.user.username;if(sub)sub.textContent=`团队空间 · ${authMode.user.role}`;}
  if(switcher){
    switcher.hidden=!(authMode.enabled&&authMode.authenticated&&workspaces.length>1);
    switcher.innerHTML=workspaces.map(item=>`<option value="${esc(item.id)}" ${item.id===authMode.user?.workspace_id?'selected':''}>${esc(item.name)}</option>`).join('');
  }
}
async function loadWorkspaces(){
  if(!authMode.enabled||!authMode.authenticated){workspaces=[];renderAuthAccount();return;}
  try{const result=await api('/api/workspaces');workspaces=Array.isArray(result.workspaces)?result.workspaces:[];renderAuthAccount();}catch{workspaces=[];renderAuthAccount();}
}
async function initializeWorkspace(){
  try{authMode=await api('/api/auth/status');}
  catch(error){authMode={...authMode,enabled:true,authenticated:false,connectionError:error.message};renderAuthGate(`连接工作台失败：${error.message}`);return false;}
  renderAuthAccount();
  if(authMode.enabled&&!authMode.authenticated){renderAuthGate();return false;}
  await loadWorkspaces();
  const root=$('#auth-root');if(root){root.hidden=true;root.innerHTML='';}
  await refresh();
  loadPdfIngestJobs().then(data=>{if(data&&(pdfIngestJobs||[]).some(job=>['queued','running'].includes(job.status)))startPdfIngestPolling();});
  restoreMetadataDraftJob();
  return true;
}
document.addEventListener('submit',async event=>{
  if(event.target.id==='auth-form'){
    event.preventDefault();const form=event.target,fields=new FormData(form),bootstrap=Boolean(authMode.bootstrap_required),button=form.querySelector('[type="submit"]'),error=$('#auth-error');
    button.disabled=true;button.textContent=bootstrap?'正在创建…':'正在登录…';if(error)error.textContent='';
    const payload={username:fields.get('username'),password:fields.get('password')};if(bootstrap){payload.bootstrap_token=fields.get('bootstrap_token');payload.display_name=fields.get('display_name');}
    try{await api(bootstrap?'/api/auth/bootstrap':'/api/auth/login',{method:'POST',body:jsonBody(payload)});const ready=await initializeWorkspace();if(ready)toast(bootstrap?'团队管理员已创建':'已登录团队工作区');}
    catch(err){if(error)error.textContent=err.message;button.disabled=false;button.textContent=bootstrap?'创建管理员并进入':'登录';}
    return;
  }
  if(event.target.id==='workspace-member-form'){
    event.preventDefault();const fields=new FormData(event.target),button=event.target.querySelector('[type="submit"]');button.disabled=true;
    try{await api('/api/workspace/members',{method:'POST',body:jsonBody({display_name:fields.get('display_name'),username:fields.get('username'),password:fields.get('password'),role:fields.get('role')})});workspaceMembersLoaded=false;settingsSnapshot=null;await loadSettingsView();toast('成员账号已创建');}
    catch(error){toast(error.message,'error');}finally{if(button.isConnected)button.disabled=false;}
  }
  if(event.target.id==='auth-password-form'){
    event.preventDefault();const fields=new FormData(event.target);
    try{await api('/api/auth/password',{method:'PATCH',body:jsonBody({current_password:fields.get('current_password'),new_password:fields.get('new_password')})});closeModal();toast('密码已更新，其他登录会话已退出');}
    catch(error){const status=$('#auth-password-error');if(status)status.textContent=error.message;}
  }
},true);
document.addEventListener('click',async event=>{
  const localLogin=event.target.closest('[data-local-login]');
  if(localLogin){
    localLogin.disabled=true;localLogin.textContent='正在进入…';
    const error=$('#auth-error');if(error)error.textContent='';
    try{await api('/api/auth/local-login',{method:'POST',body:'{}'});const ready=await initializeWorkspace();if(ready)toast('已通过本机默认用户进入');}
    catch(err){if(error)error.textContent=err.message;localLogin.disabled=false;localLogin.textContent='默认用户进入';}
    return;
  }
  if(event.target.closest('[data-auth-retry]')){authMode.connectionError='';await initializeWorkspace();return;}
  if(event.target.closest('[data-action="auth-logout"]')){try{await api('/api/auth/logout',{method:'POST',body:'{}'});}catch{}authMode={...authMode,authenticated:false,user:null};renderAuthGate();return;}
  if(event.target.closest('[data-action="auth-password"]')){openModal(`<div class="modal-head"><div><h3>修改密码</h3><p>更新后，其他已登录设备会被退出。</p></div><button class="close-button" data-close-modal>×</button></div><form id="auth-password-form"><div class="modal-body"><label>当前密码<input class="field-input" type="password" name="current_password" required autocomplete="current-password"></label><label style="margin-top:12px">新密码<input class="field-input" type="password" name="new_password" required minlength="12" maxlength="256" autocomplete="new-password"></label><small class="auth-hint">至少 12 位，并同时包含字母与数字。</small><div class="auth-error" id="auth-password-error" role="alert"></div></div><div class="modal-foot"><button class="button" type="button" data-close-modal>取消</button><button class="button primary" type="submit">更新密码</button></div></form>`);return;}
  const saveMember=event.target.closest('[data-save-member]');if(saveMember){const id=saveMember.dataset.saveMember,row=saveMember.closest('[data-member-row]'),role=row?.querySelector('[data-member-role]')?.value,active=row?.querySelector('[data-member-active]')?.checked;saveMember.disabled=true;try{await api(`/api/workspace/members/${encodeURIComponent(id)}`,{method:'PATCH',body:jsonBody({role,active})});workspaceMembersLoaded=false;settingsSnapshot=null;await loadSettingsView();toast('成员权限已更新');}catch(error){toast(error.message,'error');}finally{if(saveMember.isConnected)saveMember.disabled=false;}return;}
});
document.addEventListener('change',async event=>{
  const switcher=event.target.closest('#workspace-switcher');
  if(!switcher||!switcher.value||switcher.value===authMode.user?.workspace_id)return;
  switcher.disabled=true;
  try{await api('/api/auth/switch-workspace',{method:'POST',body:jsonBody({workspace_id:switcher.value})});activeView='home';selectedPaperId='';activeModalPaperId='';persistUiState();authMode=await api('/api/auth/status');workspaceMembersLoaded=false;workspaceMembers=[];await initializeWorkspace();toast('已切换工作区');}
  catch(error){toast(`切换工作区失败：${error.message}`,'error');renderAuthAccount();}
  finally{if(switcher.isConnected)switcher.disabled=false;}
});
async function loadSettingsView(){
  if(activeView!=='settings')setView('settings');else{settingsSnapshot=null;renderView();}
}
function newRequestId(){return globalThis.crypto?.randomUUID?.()||'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g,c=>{const value=Math.random()*16|0;return(c==='x'?value:(value&3|8)).toString(16);});}
function isMissingRunError(error){return Number(error?.status)===404;}
function clearMissingDiscoveryRun(runId){
  if(runId && activeDiscoveryRunId!==runId)return;
  const missingId=runId||activeDiscoveryRunId;
  activeDiscoveryRunId='';
  discoveryDraftRunId='';
  discoveryRestoreRunId='';
  discoveryTriageRunId='';
  if(discoveryRestoreTimer)clearTimeout(discoveryRestoreTimer);
  if(discoveryTriageTimer)clearTimeout(discoveryTriageTimer);
  discoveryRestoreTimer=null;
  discoveryTriageTimer=null;
  discoveryRestoreAttempts=0;
  discoveryTriageAttempts=0;
  discoveryResults=[];
  selectedCandidateIds.clear();
  discoveryMeta={id:missingId,status:'expired',error:'这条检索记录已不存在，已停止恢复。你仍可以从历史记录查看其他检索，或重新搜索。'};
  persistUiState();
  loadDiscoveryHistory(true);
  if(activeView==='discover')renderView();
}

async function refresh() {
  try {
    // Start this independently so a transient dashboard/database failure does
    // not prevent the AI readiness state from recovering in the background.
    loadModelConfiguration();
    const libraryRequest=activeView==='library'?libraryUrl():(activeView==='reading'?'/api/library?limit=200&status=reading,to_read,inbox,later&sort=recent':'/api/library?limit=50');
    const [stateResult,libraryResult,queryResult,runResult,jobResult,syncResult] = await Promise.allSettled([api('/api/state'),api(libraryRequest),api('/api/queries'),api('/api/runs?limit=30'),api('/api/ai-jobs?limit=30'),api('/api/sync-jobs?limit=30')]);
    const required=[stateResult,libraryResult,queryResult,runResult];
    const requiredError=required.find(item=>item.status==='rejected');
    if(requiredError)throw requiredError.reason;
    appState=stateResult.value;libraryPage=libraryResult.value;papers=libraryPage.items||[];queries=queryResult.value;runs=runResult.value;
    if(jobResult.status==='fulfilled')aiJobs=jobResult.value.jobs||[];
    if(syncResult.status==='fulfilled')syncJobs=syncResult.value||[];
    analytics = null;
    renderSidebar(); renderGlobalTaskStatus(); renderView(); if(activeView==='discover')loadDiscoveryHistory();if(activeView==='prereading')loadStudyItems(true);
    // Model configuration is independent of the library/dashboard refresh. A
    // slow or recovering database must not leave every AI control grey forever.
    loadModelConfiguration();
    scheduleGlobalAiJobsRefresh();
    if(!uiStateRestored){
      uiStateRestored=true;
      const resumeId=persistedUiState.openPaperId||'';
      if(resumeId){setTimeout(()=>openPaper(resumeId),0);}
    }
    if(activeDiscoveryRunId){setTimeout(()=>restoreDiscoveryRun(activeDiscoveryRunId),0);}
    if(activeView==='home'){
      Promise.allSettled([api('/api/recommendations?limit=8'),api('/api/briefing?days=7&limit=5')]).then(([memoryResult,briefResult])=>{
        if(memoryResult.status==='fulfilled'){
          recommendations=memoryResult.value;
          if(recommendations?.recommendation_provider?.status==='running')watchRecommendations();
        }
        if(briefResult.status==='fulfilled')briefing=briefResult.value;
        if(activeView==='home'){renderView();}
      });
    }
  } catch(e) {
    // A transient failure in one of the parallel refresh requests must not
    // erase a usable page.  Background jobs are durable and continue on the
    // server, so keep the current view (and its visible progress) in place.
    // Only the first load, while the initial skeleton is still present, needs
    // to be replaced by a full connection error state.
    const target=$('#app-view');
    const hasRenderedView=Boolean(target&&!target.querySelector('.initial-loading'));
    if(hasRenderedView){
      toast(`刷新暂时失败，后台任务仍会继续：${e.message||'请稍后重试'}`,'error');
      return;
    }
    if(target)target.innerHTML=`<div class="empty-state"><div class="empty-mark">!</div><b>工作台暂时连接不上</b>${esc(e.message)}<div style="margin-top:14px"><button class="button" onclick="refresh()">重新连接</button></div></div>`;
  }
}
function watchRecommendations(){
  if(recommendationPollTimer)clearTimeout(recommendationPollTimer);
  if(recommendationPollAttempts>=12||activeView!=='home')return;
  recommendationPollTimer=setTimeout(async()=>{
    try{
      const result=await api('/api/recommendations?limit=8');
      recommendations=result;
      if(activeView==='home')renderView();
      if(result?.recommendation_provider?.status==='running'){
        recommendationPollAttempts++;
        watchRecommendations();
      }else recommendationPollAttempts=0;
    }catch{
      recommendationPollAttempts++;
      watchRecommendations();
    }
  },1500);
}
async function restoreDiscoveryRun(runId){
  if(!runId)return;
  try{
    const [detail,result]=await Promise.all([api(`/api/runs/${encodeURIComponent(runId)}`),api(`/api/runs/${encodeURIComponent(runId)}/results`)]);
    if(activeDiscoveryRunId!==runId)return;
    discoveryMeta={...result,...detail,run:detail};
    discoveryQuery=result.query||detail.query_name||discoveryQuery;
    restoreDiscoveryDraft(result);
    if(Array.isArray(result.papers)){
      discoveryResults=result.papers;
      // A refresh can restore a run after some candidates were already
      // imported. Drop those IDs from the persisted selection so the bulk
      // import button never reports a stale count or re-imports a paper.
      const selectable=new Set(result.papers
        .filter(item=>String(item?.id||'').startsWith('candidate:')&&!item.imported_paper_id)
        .map(item=>item.id));
      selectedCandidateIds=new Set([...selectedCandidateIds].filter(id=>selectable.has(id)));
      persistUiState();
    }
    discoveryRestoreAttempts=0;
    renderGlobalTaskStatus();
    if(activeView==='discover')renderView();
    if(discoveryStatusActive(result.status))watchDiscoveryRun(runId);
    else {
      discoveryRestoreRunId='';discoveryRestoreTimer=null;persistUiState();
      if(discoveryTriageIsActive(discoveryTriageSnapshot(discoveryMeta)))watchDiscoveryTriage(runId);
      else loadDiscoveryHistory(true);
    }
  }catch(err){
    if(isMissingRunError(err)){clearMissingDiscoveryRun(runId);return;}
    if(activeDiscoveryRunId!==runId)return;
    // Opening a saved search performs two reads in parallel. A transient
    // connection reset must not turn a durable queued/running run into a
    // false failure. Retry with a bounded backoff, keeping the last known
    // state visible while the server continues working.
    discoveryRestoreAttempts++;
    if(discoveryRestoreAttempts<12){
      discoveryMeta={...(discoveryMeta||{}),id:runId,error:`正在恢复检索状态（${discoveryRestoreAttempts}/12）…`};
      if(activeView==='discover')renderView();
      discoveryRestoreTimer=setTimeout(()=>restoreDiscoveryRun(runId),Math.min(4000,700+discoveryRestoreAttempts*350));
      return;
    }
    discoveryRestoreRunId='';
    discoveryRestoreTimer=null;
    discoveryMeta={...(discoveryMeta||{}),id:runId,status:'failed',error:`无法读取检索进度：${err.message}`};
    if(activeView==='discover')renderView();
  }
}
function watchDiscoveryRun(runId){
  if(!runId||discoveryRestoreRunId===runId)return;
  if(discoveryRestoreTimer)clearTimeout(discoveryRestoreTimer);
  discoveryRestoreRunId=runId;discoveryRestoreAttempts=0;
  const tick=async()=>{
    if(activeDiscoveryRunId!==runId){if(discoveryRestoreRunId===runId)discoveryRestoreRunId='';return;}
    try{
      const [detail,result,sync]=await Promise.all([api(`/api/runs/${encodeURIComponent(runId)}`),api(`/api/runs/${encodeURIComponent(runId)}/results`),api('/api/sync-jobs?limit=20')]);
      if(activeDiscoveryRunId!==runId)return;
      discoveryMeta={...result,...detail,run:detail};syncJobs=sync||[];renderGlobalTaskStatus();
      discoveryResults=result.papers||[];discoveryQuery=result.query||discoveryQuery;
      if(['queued','running'].includes(result.status)){
        if(activeView==='discover')renderView();
        discoveryRestoreTimer=setTimeout(tick,1500);return;
      }
      discoveryRestoreTimer=null;discoveryRestoreRunId='';persistUiState();
      if(discoveryTriageIsActive(discoveryTriageSnapshot(discoveryMeta)))watchDiscoveryTriage(runId);
      else loadDiscoveryHistory(true);
      if(activeView==='discover')renderView();
    }catch(err){
      if(activeDiscoveryRunId!==runId)return;
      if(isMissingRunError(err)){clearMissingDiscoveryRun(runId);return;}
      discoveryRestoreAttempts++;
      if(discoveryRestoreAttempts<40){discoveryRestoreTimer=setTimeout(tick,900);return;}
      discoveryRestoreTimer=null;discoveryRestoreRunId='';
      discoveryMeta={...(discoveryMeta||{}),status:'failed',error:`无法读取检索进度：${err.message}`};
      if(activeView==='discover')renderView();
    }
  };
  tick();
}
function libraryUrl(offset=0){const params=new URLSearchParams({limit:'50',offset:String(offset),q:libraryQuery,status:libraryStatus,topic:libraryTopic,sort:librarySort});if(libraryFavorite)params.set('favorite','1');if(libraryHasCard)params.set('has_reading_card','1');return `/api/library?${params}`;}
