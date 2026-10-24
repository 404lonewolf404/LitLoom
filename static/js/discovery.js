// Search, candidate screening, and discovery history workflows.

async function beginDiscoverySearch(query,sources,options,plan=null){
 discoveryDraft={query,sources:[...sources],options:{...DISCOVERY_DEFAULT_OPTIONS,...options}};
 const runId=newRequestId();activeView='discover';syncViewUrl('discover');activeDiscoveryRunId=runId;discoveryDraftRunId=runId;
 selectedCandidateIds.clear();stopDiscoveryTriageWatch();discoveryTriageFilter='recommended';discoveryLibraryFilter='new';discoveryResults=[];discoveryQuery=query;
 discoveryMeta={id:runId,status:'queued',query,query_name:query,source_names:sources,papers:[],candidate_mode:true,plan:plan||{search_options:options}};
 persistUiState();renderSidebar();renderView();
 try{
  const result=await api('/api/discover',{method:'POST',body:jsonBody({query,sources,limit:25,options,request_id:runId,plan,ai_screen:options.ai_screen})});
  if(activeDiscoveryRunId!==runId){loadDiscoveryHistory(true);return;}
  discoveryMeta={...discoveryMeta,...result};persistUiState();renderView();watchDiscoveryRun(runId);loadDiscoveryHistory(true);
  toast(options.ai_screen?'后台检索中，候选完成后自动进行 AI 初筛':'后台检索中，候选由你选择后入库');
 }catch(error){if(activeDiscoveryRunId===runId){discoveryMeta={...(discoveryMeta||{}),status:'failed',error:error.message};renderView();}loadDiscoveryHistory(true);toast(error.message,'error');}
}
function captureDiscoveryFocus(){
 const target=document.activeElement;
 if(activeView!=='discover'||!target?.closest('#app-view')||!target.matches('input,select,textarea'))return null;
 return {id:target.id,name:target.name,start:target.selectionStart,end:target.selectionEnd};
}
function restoreDiscoveryFocus(focus){
 if(!focus)return;
 const target=focus.id?document.getElementById(focus.id):$(`#discover-form [name="${CSS.escape(focus.name)}"]`);
 if(target){target.focus({preventScroll:true});if(focus.start!=null&&target.setSelectionRange)try{target.setSelectionRange(focus.start,focus.end);}catch{}}
}
function discoveryResultMarkup(p){
 const candidate=String(p.id||'').startsWith('candidate:'), libraryPaperId=p.in_library_paper_id||'', inLibrary=Boolean(libraryPaperId), screen=discoveryCandidateScreen(p), group=discoveryScreenGroup(screen), feedback=discoveryCandidateFeedbackText(p);
 const badge=screen.label?`<span class="discovery-ai-badge ${group}">AI 初筛：${screen.origin==='user_feedback'?'你的判断 · ':''}${discoveryGroupInfo(group).title}${screen.score!=null?` · ${screen.score}`:''}</span>`:'<span class="discovery-ai-badge uncertain">AI 初筛：尚待筛选</span>';
 const retrieval=discoveryRetrievalInfo(p), retrievalBadge=retrieval?`<span class="result-source discovery-retrieval-badge">检索排序：${retrieval.label}${retrieval.score!=null?` · ${retrieval.score}`:''}</span>`:'';
 const recall=discoveryRecallInfo(p), recallBadge=recall?`<span class="result-source discovery-recall-badge">模型召回线索：${recall.label}</span>`:'';
 const verificationBadge=p.discovery_only?'<span class="result-source">网页线索 · 未核验</span>':p.verification_status==='verified'?'<span class="result-source">已学术核验</span>':'';
 return `<article class="discovery-result ${candidate?'is-candidate':''} ${inLibrary?'is-in-library':''}" data-ai-label="${group}">
  <div class="result-top">${candidate&&!inLibrary?`<label class="discovery-select"><input type="checkbox" data-candidate-check="${esc(p.id)}" ${selectedCandidateIds.has(p.id)?'checked':''}> 选择入库</label>`:''}<div class="result-title" ${candidate&&!inLibrary?'':`data-open-paper="${esc(libraryPaperId||p.id)}"`}>${esc(p.title)}</div><div class="result-badges">${badge}${retrievalBadge}${recallBadge}${verificationBadge}<span class="result-source">${p.seen_before?'曾经出现':'新发现'}</span>${inLibrary?'<span class="result-source discovery-library-badge">已在文献库</span>':''}</div></div>
  <div class="paper-meta">${esc([p.authors,p.year,p.venue].filter(Boolean).join(' · '))}${p.doi?` · DOI ${esc(p.doi)}`:''}</div>
  <p class="result-abstract">${esc(p.abstract||'来源未提供摘要，请打开原文核验。')}</p>
  ${screen.reason?`<div class="discovery-ai-reason"><b>${screen.origin==='user_feedback'?'用户反馈':screen.origin==='model'?'AI 判断':'核验提示'}</b> · ${esc(screen.reason)}${screen.confidence?` <small>置信度 ${esc(({high:'高',medium:'中',low:'低'})[screen.confidence]||screen.confidence)}</small>`:''}</div>`:''}
  ${feedback?`<small class="discovery-feedback-note">${esc(feedback)} · 仅适用于当前研究问题</small>`:''}
  <div class="result-actions"><span class="result-source">${esc((p.sources||[]).map(s=>SOURCE_NAME[s.source]||s.source).join(' · ')||'来源已记录')}</span>
  ${p.url?`<a class="button small" href="${esc(p.url)}" target="_blank" rel="noopener">来源页面 ↗</a>`:''}
  ${candidate?`${inLibrary?`<button class="button small" data-open-paper="${esc(libraryPaperId)}">已在库 · 查看记录</button>`:`<button class="button small primary" data-import-candidate="${esc(p.id)}">加入文献库</button>`}<button class="button small" data-discovery-feedback="${esc(p.id)}" data-discovery-decision="relevant">相关</button><button class="button small" data-discovery-feedback="${esc(p.id)}" data-discovery-decision="irrelevant">不相关</button><button class="button small" data-discovery-feedback="${esc(p.id)}" data-discovery-decision="ignored">跳过</button>${feedback?`<button class="button small" data-discovery-feedback="${esc(p.id)}" data-discovery-decision="new">撤销判断</button>`:''}`:`<button class="button small" data-status-paper="${esc(p.id)}" data-status="to_read">加入待读</button><button class="button small" data-open-paper="${esc(p.id)}">查看记录</button>`}</div>
 </article>`;
}
function discoveryScreeningPanel(){
 const triage=discoveryTriageSnapshot(), active=discoveryTriageIsActive(triage), total=Number(triage.candidate_count??triage.total??0), processed=Number(triage.processed_count??triage.screened??0), waiting=discoveryStatusActive();
 return `<section class="discovery-screening ${active?'is-running':''}" aria-live="polite"><div><b>${waiting?'检索中，候选就绪后开始 AI 初筛':discoveryTriageStatusText(triage)}</b><span>仅分析题名与摘要，最多40篇；缺摘要保留待核验。${total?` · 已处理 ${Math.min(processed,total)}/${total} 篇`:''}${triage.retry_count?` · 自动重试 ${triage.retry_count} 次`:''}${triage.partial_count?` · ${triage.partial_count} 篇待人工核验`:''}${triage.capped?' · 超出部分保留供人工查看':''}</span>${triage.error?`<small class="discovery-history-error">${esc(triage.error)}</small>`:''}${triage.model?`<small>模型：${esc(triage.model)}</small>`:''}</div>${active||waiting?'':`<button class="button small" type="button" data-discovery-triage>${discoveryTriageStatus(triage)==='success'?'重新筛选':'启动 / 重试 AI 初筛'}</button>`}</section>`;
}
function captureDiscoveryDraft(){
 const form=$('#discover-form');if(!form)return;
 const data=new FormData(form), options={};
 for(const key of ['author','venue','exclude_terms','year_from','year_to','work_type','sort','search_depth','discovery_mode','search_backend'])options[key]=data.get(key)||'';
 for(const key of ['expand_concepts','expand_relations','suppress_seen','avoid_repeats','ai_screen','ai_expand'])options[key]=data.get(key)==='on';
 options.fill_repeats=true;
 discoveryDraft={query:String(data.get('query')||''),sources:$$('#discover-form input[name="source"]:checked').map(input=>input.value),options};persistUiState();
}
function restoreDiscoveryDraft(result,force=false){
 if(!force&&discoveryDraftRunId===result.id)return;
 discoveryDraft={query:result.query||result.query_name||'',sources:result.source_names?.length?result.source_names:['arxiv','openalex'],options:{...DISCOVERY_DEFAULT_OPTIONS,...(result.plan?.search_options||result.plan?.filters||{})}};
 discoveryDraftRunId=result.id||'';persistUiState();
}
function renderDiscover(){
 const options={...DISCOVERY_DEFAULT_OPTIONS,...discoveryDraft.options}, sourceChoices=Object.entries(SOURCE_NAME).filter(([id])=>!['manual','import'].includes(id));
 const input=(name,placeholder,type='text')=>`<input class="field-input" name="${name}" type="${type}" value="${esc(options[name]??'')}" placeholder="${placeholder}" ${type==='number'?'min="1800" max="2200"':''}>`;
 const select=(name,values)=>`<select class="field-select" name="${name}">${values.map(([value,label])=>`<option value="${value}" ${String(options[name]||'')===value?'selected':''}>${label}</option>`).join('')}</select>`;
 const check=(name,label)=>`<label class="check-label"><input type="checkbox" name="${name}" ${options[name]?'checked':''}> ${label}</label>`;
 const candidateMode=Boolean(discoveryMeta?.candidate_mode), counts={all:discoveryResults.length,direct:0,adjacent:0,uncertain:0,unrelated:0}, libraryCounts={all:discoveryResults.length,new:0,in_library:0};
 discoveryResults.forEach(p=>counts[discoveryScreenGroup(discoveryCandidateScreen(p))]++);
 discoveryResults.forEach(p=>libraryCounts[p.in_library_paper_id?'in_library':'new']++);
 const ordered=[...discoveryResults].sort((a,b)=>discoveryGroupInfo(discoveryScreenGroup(discoveryCandidateScreen(a))).rank-discoveryGroupInfo(discoveryScreenGroup(discoveryCandidateScreen(b))).rank);
 counts.recommended=counts.all-counts.unrelated;
 const visible=ordered.filter(p=>(discoveryTriageFilter==='all'||(discoveryTriageFilter==='recommended'?discoveryScreenGroup(discoveryCandidateScreen(p))!=='unrelated':discoveryScreenGroup(discoveryCandidateScreen(p))===discoveryTriageFilter))&&(!candidateMode||discoveryLibraryFilter==='all'||(discoveryLibraryFilter==='in_library'?Boolean(p.in_library_paper_id):!p.in_library_paper_id)));
 const triageFilters=discoveryResults.length?`<div class="discovery-filters">${['recommended','direct','adjacent','uncertain','unrelated','all'].map(key=>`<button type="button" class="discovery-filter ${discoveryTriageFilter===key?'active':''}" data-discovery-filter="${key}">${key==='all'?'全部':key==='recommended'?'推荐与待核验':discoveryGroupInfo(key).title} ${counts[key]}</button>`).join('')}</div>`:'';
 const libraryFilters=candidateMode&&discoveryResults.length?`<div class="discovery-filters discovery-library-filters"><span>文献库状态</span>${[['new','仅看新候选'],['in_library','已在文献库'],['all','全部候选']].map(([key,label])=>`<button type="button" class="discovery-filter ${discoveryLibraryFilter===key?'active':''}" data-discovery-library-filter="${key}">${label} ${libraryCounts[key]}</button>`).join('')}</div>`:'';
 const filters=`${libraryFilters}${triageFilters}`;
 const empty=discoveryStatusActive()?emptyState('正在检索论文','后台进度会持续更新；刷新后可继续查看。'):discoveryResults.length&&candidateMode&&discoveryLibraryFilter==='new'&&!libraryCounts.new?emptyState('候选均已在文献库','切换“已在文献库”可打开已有记录；候选总数仍保留。'):discoveryResults.length?emptyState('此筛选下没有候选','可以切换其他分组或查看“全部候选”。'):discoveryMeta?emptyState('本次没有可显示的候选',discoveryMeta.error||'可能被年份、排除词或“只看未重复”过滤。可查看历史记录或调整条件重搜。'):emptyState('输入研究问题，开始发现','检索后先查看 AI 建议，再选择加入文献库。');
 const plan=discoveryMeta?.plan||{}, details=discoveryMeta?.run?.source_details||discoveryMeta?.sources||[], summary=discoveryMeta?.summary||{}, qualityFiltered=Number(summary.model_web_quality_filtered||0), modelBackfilled=Number(summary.model_web_backfilled||0);
  const localFill=Number(summary.model_web_local_fill||0), requestedCount=Number(summary.result_limit||discoveryMeta?.requested_count||0), shortfallCount=Number(summary.shortfall_count||discoveryMeta?.shortfall_count||0);
 const quotaMessage=requestedCount?(shortfallCount?`目标 ${requestedCount} · 实际 ${discoveryResults.length} · 缺少 ${shortfallCount}：${summary.shortfall_reason||'可核验候选不足，未用无关论文凑数'}`:`目标 ${requestedCount} · 实际 ${discoveryResults.length} · 数量已满足`):'';
 const repeatPolicy=summary.repeat_policy==='strict_fresh_only'?'严格只保留新论文':summary.repeat_policy==='new_first_history_fill'?'新论文优先，数量不足时历史候选补位':'按相关性排序，不额外偏好重复策略';
 const planMarkup=discoveryMeta?`<div class="search-intelligence"><div><b>本次研究问题</b><span>${esc(discoveryMeta.query||discoveryQuery)}</span></div><div><b>数量保障</b><span>${esc(quotaMessage||'等待检索完成')}${summary.repeat_fill_count?` · 历史补位 ${summary.repeat_fill_count} 篇`:''}</span></div><div><b>检索与去重</b><span>${esc(repeatPolicy)} · 原始 ${discoveryMeta.total_fetched||0} · 条件过滤 ${discoveryMeta.total_filtered||0} · 重复合并 ${discoveryMeta.duplicate_count||0} · 候选 ${discoveryResults.length}${qualityFiltered?` · 相关性门槛隐藏 ${qualityFiltered}`:''} · 首次出现 ${summary.new_to_topic||0}${modelBackfilled?` · AI 候选补位 ${modelBackfilled}`:''}${localFill?` · 本地检索补位 ${localFill}`:''}</span></div><div><b>检索表达</b><span>${esc((plan.query_variants||[]).slice(0,4).map(item=>typeof item==='string'?item:item?.query||'').filter(Boolean).join(' · ')||plan.strategy||'按当前条件检索')}</span></div><div><b>来源状态</b><span>${esc(details.map(s=>typeof s==='string'?SOURCE_NAME[s]||s:`${SOURCE_NAME[s.source]||s.source} ${s.status==='ok'?'✓':'失败'}`).join(' · '))}</span></div></div>`:'';
  return `${viewHead('LITERATURE DISCOVERY','发现论文','检索结果先进入候选页；AI 判断相关性，你决定哪些加入文献库。',`${candidateMode?`<button class="button primary" type="button" data-import-selected-candidates ${selectedCandidateIds.size?'':'disabled'}>加入选中候选 <span id="discovery-selection-count">${selectedCandidateIds.size?`(${selectedCandidateIds.size})`:''}</span></button>`:''}<button class="button" data-action="add-paper">＋ 手动添加</button>`)}
  <div class="grid view-grid"><div>${discoveryHistoryPanelMarkup()}<section class="search-panel"><form id="discover-form"><div class="search-form"><input class="field-input" name="query" value="${esc(discoveryDraft.query??'')}" placeholder="说明研究对象、任务和方法，例如：用视觉语言模型检测工业缺陷" required><button class="button primary" type="submit">⌕ 搜索论文</button><button class="button" type="button" data-action="agent-search-plan">✦ AI 扩展检索词</button></div><details class="search-settings"><summary>检索设置 <span>来源、AI 初筛与高级条件</span></summary><div class="search-settings-body"><div class="search-options search-sources">${sourceChoices.map(([id,label])=>`<label class="check-label"><input type="checkbox" name="source" value="${id}" ${(discoveryDraft.sources||[]).includes(id)?'checked':''}> ${label}</label>`).join('')}<small class="discovery-search-help">Tavily 是可选网络发现来源，需要本地配置 Key；未能由学术来源核验的网页只作线索，候选不会自动入库。</small></div><div class="search-options">${check('ai_screen','AI 相关性初筛')}${check('ai_expand','AI 语义扩展（仅学术数据库模式）')}${check('expand_concepts','概念扩展')}${check('expand_relations','沿馆藏邻域扩展')}${check('suppress_seen','优先新论文（不足时补历史）')}</div><div class="search-advanced">${input('author','作者过滤')}${input('venue','会议 / 期刊过滤')}${input('exclude_terms','排除词，用逗号分隔')}${input('year_from','起始年份','number')}${input('year_to','结束年份','number')}${select('work_type',[['','全部类型'],['journal','期刊'],['conference','会议'],['review','综述'],['preprint','预印本']])}${select('sort',[['relevance','综合相关性'],['newest','最新优先'],['novelty','新发现优先'],['foundations','经典优先'],['oldest','最早优先']])}${select('search_depth',[['focused','精准检索'],['balanced','平衡检索'],['deep','深度检索']])}${select('discovery_mode',[['balanced','平衡发现'],['latest','前沿优先'],['foundations','基础优先'],['explore','邻域探索']])}</div></div></details></form></section>${planMarkup}${candidateMode?discoveryScreeningPanel():''}<div class="result-head"><span>候选结果 · ${visible.length} / ${discoveryResults.length} 篇 · 新候选 ${libraryCounts.new} · 已在库 ${libraryCounts.in_library}</span><span>结果会完整保留 · 已入库论文可切换查看</span></div>${filters}<div class="surface-card">${visible.length?visible.map(discoveryResultMarkup).join(''):empty}</div></div><aside class="surface-card filter-card"><h4>如何筛选</h4><div class="discovery-sidebar-help"><p>① 等候选出现，可立即人工查看。</p><p>② AI 分为直接相关、邻近相关、待核验、不相关，并给出理由。</p><p>③ 勾选需要的论文，点击“加入选中候选”。刷新不会清掉勾选。</p><p>④ 标记相关 / 不相关，反馈按研究问题保存；换方向时不会沿用旧主题的排除判断。</p><p>⑤ 默认优先新论文；数量不足时会补入同一研究问题的历史候选并明确标记。需要严格排除历史结果时再启用“只看未重复” API 选项。</p></div></aside></div>`;
}

/* Candidate triage uses the current Responses-compatible backend contract.
   The older renderer above remains the base layout; this layer only turns its
   results into a persistent review queue. */
function discoveryTriageSnapshot(meta=discoveryMeta){
 const state=meta?.triage||meta?.summary?.triage||meta?.ai_screening||meta?.summary?.ai_screening;
 return state&&typeof state==='object'?state:{};
}
function discoveryTriageStatus(state=discoveryTriageSnapshot()){
 const value=String(state?.status||'').toLowerCase();
 return value==='completed'?'success':value;
}
function discoveryTriageIsActive(state=discoveryTriageSnapshot()){return ['queued','running'].includes(discoveryTriageStatus(state));}
function discoveryTriageStatusText(state=discoveryTriageSnapshot()){
 return ({queued:'排队中',running:'AI 初筛中',success:'AI 初筛完成',partial:'AI 初筛部分完成',failed:'AI 初筛失败',unavailable:'AI 初筛不可用',interrupted:'重启中断，可重试',cancelled:'AI 初筛已取消'})[discoveryTriageStatus(state)]||'尚未进行 AI 初筛';
}
function discoveryCandidateScreen(paper={}){
 const decision=paper.review_decision||paper.user_decision;
 if(['relevant','irrelevant','ignored'].includes(decision))return {label:decision==='relevant'?'direct':decision==='irrelevant'?'unrelated':'uncertain',origin:'user_feedback',reason:decision==='relevant'?'你已确认与当前研究问题相关。':decision==='irrelevant'?'你已确认与当前研究问题不相关。':'你选择暂时跳过。'};
 const screen=paper.ai_triage||paper.ai_screening||paper.screening||{};
 return screen&&typeof screen==='object'?screen:{};
}
function discoveryRecallInfo(paper={}){
 const value=String(paper.model_relevance_label||'').toLowerCase();
 const labels={direct:'直接相关',adjacent:'邻近相关',uncertain:'待核验',unrelated:'不相关'};
 return labels[value]?{label:labels[value],value}:null;
}
function discoveryRetrievalInfo(paper={}){
 const raw=paper.retrieval_relevance_label||paper.discovery_class||paper.relevance_label||'';
 const value=String(raw).toLowerCase();
 const labels={
  '高相关':'高相关','high':'高相关','high_relevance':'高相关','direct':'高相关',
  '邻近方向':'邻近方向','邻近相关':'邻近方向','adjacent':'邻近方向','medium':'邻近方向',
  '跨领域线索':'跨领域线索','不相关':'不相关','unrelated':'不相关','uncertain':'待核验'
 };
 const label=labels[value]||String(raw||'').trim();
 if(!label)return null;
 const score=paper.retrieval_relevance_score??(paper.relevance_score!=null?paper.relevance_score:null);
 return {label,score:score==null||score===''?null:score};
}
function discoveryScreenGroup(screen={}){
 const value=String(screen.label||'').toLowerCase();
 if(['priority','direct','high','high_relevance'].includes(value))return 'direct';
 if(['possible','adjacent','nearby','medium'].includes(value))return 'adjacent';
 if(['exclude','unrelated','irrelevant','low'].includes(value))return 'unrelated';
 return 'uncertain';
}
function discoveryGroupInfo(group){return ({direct:{title:'优先查看',rank:0},adjacent:{title:'可能相关',rank:1},uncertain:{title:'待核验',rank:2},unrelated:{title:'建议排除',rank:3}})[group]||{title:'待核验',rank:2};}
function discoveryCandidateFeedbackText(paper={}){
 const decision=String(paper.review_decision||paper.user_decision||'').toLowerCase(), prior=paper.prior_feedback;
 const priorDecision=typeof prior==='string'?prior:String(prior?.decision||'').toLowerCase();
 if(decision==='relevant')return '你已标记相关';
 if(decision==='irrelevant')return '你已标记不相关';
 if(decision==='ignored')return '你已跳过';
 if(priorDecision==='relevant')return '此前曾标记相关';
 if(priorDecision==='irrelevant')return '此前已标记不相关';
 if(priorDecision==='ignored')return '此前曾跳过';
 return '';
}
function discoveryHistorySources(item={}){return (Array.isArray(item.sources)?item.sources:[]).map(source=>SOURCE_NAME[source?.source||source]||source?.source||source).filter(Boolean).join('、');}
function discoveryHistoryPanelMarkup(){
 const rows=(discoveryHistory||[]).slice(0,20).map(item=>{
  const id=String(item.id||item.run_id||''), count=Number(item.result_count??item.candidate_count??0), imported=Number(item.imported_count??0), triage=item.triage||item.ai_screening||{};
  const displayStatus=item.error&&count?'partial':item.status;
  const statusNote=item.error&&count?'（结果已保存，持久化有异常）':'';
  return `<div class="discovery-history-item ${id===activeDiscoveryRunId?'active':''}"><span><b>${esc(item.query||item.query_name||'未命名检索')}</b><small>${esc(timeAgo(item.created_at||item.started_at||item.updated_at))} · ${esc(discoveryStatusLabel(displayStatus))}${esc(statusNote)} · 候选 ${count} · 已导入 ${imported}${discoveryHistorySources(item)?` · ${esc(discoveryHistorySources(item))}`:''}</small><small>${esc(discoveryTriageStatusText(triage))}</small>${item.error||triage.error?`<small class="discovery-history-error">${esc(item.error||triage.error)}</small>`:''}</span><span class="discovery-history-actions"><button type="button" class="button small" data-discovery-history-run="${esc(id)}">查看</button><button type="button" class="button small" data-discovery-history-rerun="${esc(id)}">重搜</button></span></div>`;
 }).join('');
 return `<details class="discovery-history" open><summary><span><b>历史搜索</b><small>查看可恢复旧结果；重搜使用原条件</small></span><button type="button" class="text-link" data-refresh-discovery-history>刷新</button></summary><div class="discovery-history-toolbar"><input class="field-input" id="discovery-history-search" value="${esc(discoveryHistorySearch)}" placeholder="查找以前的研究问题"><button class="button small" data-history-page="previous" ${discoveryHistoryOffset?'':'disabled'}>上一页</button><span>第 ${Math.floor(discoveryHistoryOffset/20)+1} 页</span><button class="button small" data-history-page="next" ${discoveryHistory.length===20?'':'disabled'}>下一页</button></div>${discoveryHistoryError?`<div class="discovery-history-error">读取历史失败：${esc(discoveryHistoryError)}</div>`:''}<div class="discovery-history-list">${rows||`<div class="discovery-history-empty">${discoveryHistoryLoaded?'暂无匹配的搜索记录':'正在读取历史搜索…'}</div>`}</div></details>`;
}
function renderDiscoveryHistoryPanel(){const panel=$('#app-view .discovery-history');if(panel){const focus=captureDiscoveryFocus(),open=panel.open;panel.outerHTML=discoveryHistoryPanelMarkup();$('#app-view .discovery-history').open=open;restoreDiscoveryFocus(focus);}}
async function loadDiscoveryHistory(force=false){
 if(discoveryHistoryLoading)return discoveryHistoryLoading;
 if(!force&&discoveryHistoryLoaded&&Date.now()-discoveryHistoryFetchedAt<15000)return discoveryHistory;
 const historyKey=JSON.stringify([discoveryHistoryOffset,discoveryHistorySearch]);
 const params=new URLSearchParams({limit:'20',offset:String(discoveryHistoryOffset),q:discoveryHistorySearch});
 const request=api('/api/discover/history?'+params).then(data=>{
  if(historyKey!==JSON.stringify([discoveryHistoryOffset,discoveryHistorySearch]))return discoveryHistory;
  discoveryHistory=Array.isArray(data)?data:(data.items||data.runs||data.history||[]);discoveryHistoryLoaded=true;discoveryHistoryFetchedAt=Date.now();discoveryHistoryError='';
  if(activeView==='discover')renderDiscoveryHistoryPanel();return discoveryHistory;
 }).catch(error=>{discoveryHistoryError=error.message||'请求失败';discoveryHistoryLoaded=true;if(activeView==='discover')renderDiscoveryHistoryPanel();return discoveryHistory;});
 discoveryHistoryLoading=request;
 try{return await request;}finally{if(discoveryHistoryLoading===request)discoveryHistoryLoading=null;if(historyKey!==JSON.stringify([discoveryHistoryOffset,discoveryHistorySearch]))loadDiscoveryHistory(true);}
};
async function openDiscoveryHistory(runId){
 if(!runId)return;
 selectedCandidateIds.clear();stopDiscoveryTriageWatch();discoveryDraftRunId='';
  activeView='discover';syncViewUrl('discover');activeDiscoveryRunId=runId;discoveryResults=[];discoveryMeta={id:runId,status:'queued',candidate_mode:true};persistUiState();renderView();
 await restoreDiscoveryRun(runId);
};
async function rerunDiscoveryHistory(runId,button){
 if(!runId)return;const originalLabel=button?.textContent||'重搜';if(button){button.disabled=true;button.textContent='重搜中…';}
 closeModal();
 try{
  const aiScreen=$('#discover-form [name="ai_screen"]')?.checked!==false;
  const response=await api(`/api/runs/${encodeURIComponent(runId)}/rerun`,{method:'POST',body:jsonBody({ai_screen:aiScreen})});
  const run=response?.run||response;activeView='discover';activeDiscoveryRunId=run.id||run.run_id||newRequestId();selectedCandidateIds.clear();stopDiscoveryTriageWatch();restoreDiscoveryDraft(run,true);discoveryResults=run.papers||[];discoveryQuery=run.query||run.query_name||discoveryQuery;discoveryMeta={...run,id:activeDiscoveryRunId,candidate_mode:true,status:run.status||'queued'};persistUiState();renderView();
  if(discoveryStatusActive(discoveryMeta.status))watchDiscoveryRun(activeDiscoveryRunId);else if(discoveryTriageIsActive(discoveryTriageSnapshot()))watchDiscoveryTriage(activeDiscoveryRunId);
  loadDiscoveryHistory(true);toast('已按原条件创建新的候选搜索，不会自动加入文献库');
 }catch(error){toast(`重搜失败：${error.message}`,'error');}
 finally{if(button?.isConnected){button.disabled=false;button.textContent=originalLabel;}}
}
async function startDiscoveryTriage(force=false){
 if(!activeDiscoveryRunId){toast('请先完成一次候选搜索','error');return;}
 try{
  const response=await api(`/api/runs/${encodeURIComponent(activeDiscoveryRunId)}/triage`,{method:'POST',body:jsonBody({force:Boolean(force)})});
  const triage=response?.triage||response;discoveryMeta={...(discoveryMeta||{}),triage};renderView();
  if(discoveryTriageIsActive(triage))watchDiscoveryTriage(activeDiscoveryRunId);else loadDiscoveryHistory(true);
 }catch(error){toast(`AI 初筛未启动：${error.message}`,'error');}
}
function stopDiscoveryTriageWatch(){if(discoveryTriageTimer)clearTimeout(discoveryTriageTimer);discoveryTriageTimer=null;discoveryTriageRunId='';discoveryTriageAttempts=0;}
function watchDiscoveryTriage(runId){
 if(!runId||discoveryTriageRunId===runId)return;stopDiscoveryTriageWatch();discoveryTriageRunId=runId;
 const tick=async()=>{
  if(activeDiscoveryRunId!==runId){if(discoveryTriageRunId===runId)stopDiscoveryTriageWatch();return;}
  try{
   const result=await api(`/api/runs/${encodeURIComponent(runId)}/results`);
   if(activeDiscoveryRunId!==runId)return;
   discoveryMeta={...(discoveryMeta||{}),...result};if(Array.isArray(result.papers))discoveryResults=result.papers;discoveryQuery=result.query||discoveryQuery;
   if(activeView==='discover')renderView();
   if(discoveryTriageIsActive(discoveryTriageSnapshot())){discoveryTriageAttempts=0;discoveryTriageTimer=setTimeout(tick,1400);return;}
   stopDiscoveryTriageWatch();loadDiscoveryHistory(true);
  }catch(error){
   if(activeDiscoveryRunId!==runId)return;
   if(isMissingRunError(error)){clearMissingDiscoveryRun(runId);return;}
   discoveryTriageAttempts++;if(discoveryTriageAttempts<24){discoveryTriageTimer=setTimeout(tick,1800);return;}
   discoveryMeta={...(discoveryMeta||{}),triage:{...(discoveryTriageSnapshot()||{}),status:'failed',error:`无法读取 AI 初筛状态：${error.message}`}};stopDiscoveryTriageWatch();if(activeView==='discover')renderView();
  }
 };
 tick();
}
async function submitDiscoveryFeedback(candidateId,decision,button){
 if(!activeDiscoveryRunId||!candidateId)return;if(button)button.disabled=true;
 try{
  const data=await api(`/api/runs/${encodeURIComponent(activeDiscoveryRunId)}/candidates/${encodeURIComponent(candidateId)}/feedback`,{method:'POST',body:jsonBody({decision})});
  const replacement=data?.candidate||data?.item||{};
  discoveryResults=discoveryResults.map(item=>item.id===candidateId?{...item,...replacement,review_decision:replacement.review_decision||decision,user_decision:replacement.user_decision||decision,prior_feedback:Object.prototype.hasOwnProperty.call(replacement,'prior_feedback')?replacement.prior_feedback:item.prior_feedback}:item);
  renderView();loadDiscoveryHistory(true);toast(({relevant:'已记为相关，后续相似搜索会参考',irrelevant:'已记为不相关，后续同类搜索会减少推荐',ignored:'已跳过这篇候选'})[decision]||'已保存反馈');
 }catch(error){if(button)button.disabled=false;toast(`保存反馈失败：${error.message}`,'error');}
};

