// User actions, imports, background task controls, and app startup.

document.addEventListener('click',async e=>{
 const semanticBuild=e.target.closest('[data-semantic-index]');if(semanticBuild){const id=semanticBuild.dataset.semanticIndex,consent=$('#semantic-index-consent');if(!consent?.checked){toast('请先确认本次文字外发授权','error');return;}semanticBuild.disabled=true;try{const result=await api(`/api/papers/${encodeURIComponent(id)}/semantic-index`,{method:'POST',body:jsonBody({consent:true})});if(result.job){toast(result.deduplicated?'该论文的语义索引任务已在队列中':'语义索引已加入后台队列');renderSemanticIndexStatus(id,{...(await api(`/api/papers/${encodeURIComponent(id)}/semantic-index`))});}else{toast('语义索引已是最新');renderSemanticIndexStatus(id,result.status||{});}}catch(err){toast(err.message,'error');}finally{if(semanticBuild.isConnected)await loadSemanticIndexStatus(id);}return;}
 const semanticCancel=e.target.closest('[data-semantic-index-cancel]');if(semanticCancel){semanticCancel.disabled=true;try{const job=await api(`/api/semantic-index-jobs/${encodeURIComponent(semanticCancel.dataset.semanticIndexCancel)}/cancel`,{method:'POST',body:jsonBody({})});toast(job.status==='cancelled'?'索引已取消':'已请求取消索引');}catch(err){toast(err.message,'error');}return;}
 const agentGoal=e.target.closest('[data-agent-goal]');if(agentGoal){activeAgentGoalId=agentGoal.dataset.agentGoal;localStorage.setItem('researchdesk.agent-goal',activeAgentGoalId);activeAgentRun=null;await loadAgentWorkspace();return;}
 const agentActionButton=e.target.closest('[data-agent-action]');if(agentActionButton){await agentAction(agentActionButton.dataset.agentAction);return;}
 if(e.target.closest('[data-agent-schedule-toggle]')){await toggleAgentSchedule();return;}
 if(e.target.closest('[data-agent-new-run]')){if(!activeAgentGoalId){toast('请先选择研究目标','error');return;}try{activeAgentRun=await api(`/api/research-goals/${encodeURIComponent(activeAgentGoalId)}/runs`,{method:'POST',body:jsonBody({trigger:'manual'})});renderView();toast('新的运行计划已生成');}catch(err){toast(err.message,'error');}return;}
 if(e.target.closest('[data-agent-import]')){await importAgentSelection();return;}
 if(e.target.closest('[data-agent-download]')){await downloadAgentSelection();return;}
 const applyCardButton=e.target.closest('[data-agent-apply-card]');if(applyCardButton){await applyAgentCard(applyCardButton.dataset.agentApplyCard);return;}
 if(e.target.closest('[data-agent-export-report]')){await exportAgentReport();return;}
 if(e.target.closest('[data-agent-scroll-form]')){$('#agent-goal-form')?.scrollIntoView({behavior:'smooth',block:'center'});$('#agent-goal-form input[name="name"]')?.focus();return;}
 const nav=e.target.closest('[data-nav]');if(nav){setView(nav.dataset.nav);return;}
 const navItem=e.target.closest('.nav-item[data-view]');if(navItem){setView(navItem.dataset.view);return;}
 const enterDeepReadButton=e.target.closest('[data-enter-deepread]');if(enterDeepReadButton){enterDeepRead(enterDeepReadButton.dataset.enterDeepread);return;}
 const openReadingPaper=e.target.closest('[data-open-reading-paper]');if(openReadingPaper){selectedPaperId=openReadingPaper.dataset.openReadingPaper;closeModal();persistUiState();setView('reading');return;}
 const deepReadPaper=e.target.closest('[data-deepread-paper]');if(deepReadPaper){deepReadPaperId=deepReadPaper.dataset.deepreadPaper;selectedPaperId=deepReadPaperId;deepReadState={...deepReadState,loading:false,error:'',paper:null,analyses:[],jobs:[]};persistUiState();loadDeepReadWorkspace(deepReadPaperId,true);return;}
 const deepReadFilter=e.target.closest('[data-deepread-filter]');if(deepReadFilter){const previousFilter=deepReadState.filter||'overview';deepReadState.filter=deepReadFilter.dataset.deepreadFilter||'overview';persistUiState();if(deepReadState.filter==='paragraph'){if(!(deepReadState.segments||[]).length)loadReadingSegments(deepReadPaperId);else renderView();}else if(previousFilter==='paragraph'){renderView();setTimeout(renderDeepReadArtifact,0);}else{document.querySelectorAll('[data-deepread-filter]').forEach(item=>item.classList.toggle('active',item===deepReadFilter));applyDeepReadFilter();}return;}
 const paragraphIndex=e.target.closest('[data-paragraph-index]');if(paragraphIndex){deepReadState.segmentIndex=Number(paragraphIndex.dataset.paragraphIndex||0);persistUiState();renderView();return;}
 const paragraphPrev=e.target.closest('[data-paragraph-prev]');if(paragraphPrev){deepReadState.segmentIndex=Math.max(0,Number(deepReadState.segmentIndex||0)-1);persistUiState();renderView();return;}
 const paragraphNext=e.target.closest('[data-paragraph-next]');if(paragraphNext){deepReadState.segmentIndex=Math.min((deepReadState.segments||[]).length-1,Number(deepReadState.segmentIndex||0)+1);persistUiState();renderView();return;}
 const paragraphAnalyze=e.target.closest('[data-paragraph-analyze]');if(paragraphAnalyze){await requestParagraphAnalysis(paragraphAnalyze.dataset.paragraphAnalyze);return;}
 const deepReadGenerate=e.target.closest('[data-deepread-generate]');if(deepReadGenerate){await requestDeepReadStage(deepReadGenerate.dataset.deepreadGenerate,'deep_read');return;}
 const deepReadCard=e.target.closest('[data-deepread-card]');if(deepReadCard){await requestDeepReadStage(deepReadCard.dataset.deepreadCard,'reading_card');return;}
 const deepReadPrereading=e.target.closest('[data-deepread-prereading]');if(deepReadPrereading){await requestDeepReadStage(deepReadPrereading.dataset.deepreadPrereading,'prereading_guide');return;}
 if(e.target.closest('[data-dismiss-modal]')&&e.target.classList.contains('modal-backdrop')){closeModal();return;}
 if(e.target.closest('[data-close-modal]')){closeModal();return;}
 const open=e.target.closest('[data-open-paper]');if(open){await openPaper(open.dataset.openPaper);return;}
 const action=e.target.closest('[data-action]')?.dataset.action;
  if(action==='add-paper'){addPaperModal();return;}if(action==='add-query'){addQueryModal();return;}if(action==='import'){importModal();return;}if(action==='backup'){createBackup();return;}if(action==='agent-config'){agentConfigModal();return;}if(action==='refresh-analytics'){analytics=null;renderView();return;}if(action==='refresh-settings'){settingsSnapshot=null;renderView();return;}
  if(action==='save-backup-policy'){const button=e.target.closest('[data-action="save-backup-policy"]');button.disabled=true;try{settingsSnapshot.backupPolicy=await api('/api/settings/backup-policy',{method:'PATCH',body:jsonBody({enabled:$('#backup-enabled')?.checked===true,interval_hours:Number($('#backup-interval')?.value),retention:Number($('#backup-retention')?.value)})});settingsSnapshot.maintenance=await api('/api/maintenance/status');renderView();toast('自动备份设置已保存');}catch(err){toast(err.message,'error');}finally{if(button.isConnected)button.disabled=false;}return;}
  if(action==='rebuild-search-index'){try{const result=await api('/api/maintenance/rebuild-search-index',{method:'POST',body:'{}'});settingsSnapshot=null;renderView();toast(`全文索引已重建：${result.documents} 篇`);}catch(err){toast(err.message,'error');}return;}
 if(action==='restore-backup'){await restoreBackup();return;}
  if(e.target.closest('[data-library-load-more]')){await loadLibraryPage(false);return;}
 const deletePaper=e.target.closest('[data-delete-paper]');
 if(deletePaper){
   const id=deletePaper.dataset.deletePaper;
   const paper=(libraryPage.items||[]).find(item=>item.id===id)||papers.find(item=>item.id===id);
   if(!paper)return;
   if(!confirm(`确定永久删除《${paper.title||'这篇论文'}》吗？\n阅读卡、笔记、AI 分析和本地 PDF 也会删除。`))return;
   deletePaper.disabled=true;
   try{const result=await api(`/api/papers/${encodeURIComponent(id)}`,{method:'DELETE',body:jsonBody({})});selectedLibraryIds.delete(id);await refresh();if(activeModalPaperId===id)closeModal();toast(result.deleted?'论文及其本地数据已删除':'论文未删除','');}
   catch(err){deletePaper.disabled=false;toast(`删除失败：${err.message}`,'error');}
   return;
 }
 const batchDelete=e.target.closest('[data-batch-delete]');
 if(batchDelete){
   const ids=[...selectedLibraryIds];
   if(!ids.length){toast('请先选择要删除的论文','error');return;}
   if(!confirm(`确定永久删除选中的 ${ids.length} 篇论文吗？\n收藏论文会被保护，其余论文的阅读卡、笔记、AI 分析和本地 PDF 会删除。`))return;
   batchDelete.disabled=true;
   try{const result=await api('/api/papers/batch',{method:'DELETE',body:jsonBody({ids,protect_favorites:true})});selectedLibraryIds.clear();await refresh();toast(`已删除 ${result.deleted||0} 篇${result.protected?.length?`，保护收藏 ${result.protected.length} 篇`:''}`);}
   catch(err){toast(`批量删除失败：${err.message}`,'error');}
   finally{if(batchDelete.isConnected)batchDelete.disabled=false;}
   return;
 }
 const runDetail=e.target.closest('[data-run-detail]');if(runDetail){await showRunDetail(runDetail.dataset.runDetail);return;}
  const history=e.target.closest('[data-paper-history]');if(history){await showPaperHistory(history.dataset.paperHistory);return;}
  const restoreRevision=e.target.closest('[data-restore-revision]');if(restoreRevision){if(!confirm('恢复此版本会替换当前笔记或阅读卡，并保存一条新的历史记录。继续吗？'))return;try{await api(`/api/papers/${restoreRevision.dataset.restorePaper}/history/${restoreRevision.dataset.restoreRevision}/restore`,{method:'POST',body:'{}'});closeModal();await refresh();await openPaper(restoreRevision.dataset.restorePaper);toast('历史版本已恢复');}catch(err){toast(err.message,'error');}return;}
 const saveCard=e.target.closest('[data-save-reading-card]');if(saveCard){await saveReadingCard(saveCard.dataset.saveReadingCard,saveCard);return;}
 const libraryCheck=e.target.closest('[data-library-check]');if(libraryCheck){if(libraryCheck.checked)selectedLibraryIds.add(libraryCheck.dataset.libraryCheck);else selectedLibraryIds.delete(libraryCheck.dataset.libraryCheck);renderView();return;}
  const selectAll=e.target.closest('[data-library-select-all]');if(selectAll){(libraryPage.items||[]).forEach(p=>selectAll.checked?selectedLibraryIds.add(p.id):selectedLibraryIds.delete(p.id));renderView();return;}
 const batch=e.target.closest('[data-batch-action]');if(batch){const actionName=batch.dataset.batchAction;if(actionName==='favorite')await updateSelectedPapers({is_favorite:true});else if(actionName==='topic'){const topic=$('.batch-topic')?.value?.trim();if(!topic){toast('请先填写批量主题','error');return;}await updateSelectedPapers({topic});}else await updateSelectedPapers({status:actionName});return;}
 const run=e.target.closest('[data-run-query]');if(run){runSavedQuery(run.dataset.runQuery);return;}
 const toggle=e.target.closest('[data-toggle-query]');if(toggle){try{await api(`/api/queries/${toggle.dataset.toggleQuery}`,{method:'PATCH',body:jsonBody({active:toggle.dataset.active==='1'})});await refresh();toast(toggle.dataset.active==='1'?'主题追踪已启用':'主题追踪已暂停');}catch(err){toast(err.message,'error');}return;}
 const qrun=e.target.closest('[data-query-run]');if(qrun){runSavedQuery(qrun.dataset.queryRun);return;}
 const selectPaper=e.target.closest('[data-select-paper]');if(selectPaper){selectedPaperId=selectPaper.dataset.selectPaper;renderView();return;}
  const openFavorite=e.target.closest('[data-favorite]');if(openFavorite){const p=(libraryPage.items||[]).find(x=>x.id===openFavorite.dataset.favorite)||papers.find(x=>x.id===openFavorite.dataset.favorite);if(p){await api(`/api/papers/${p.id}`,{method:'PATCH',body:jsonBody({is_favorite:!p.is_favorite})});await refresh();}return;}
 const saveRecommendation=e.target.closest('[data-save-recommendation],[data-save-openalex-recommendation]');if(saveRecommendation){saveRecommendation.disabled=true;try{const result=await api('/api/recommendations/import',{method:'POST',body:jsonBody({external_id:saveRecommendation.dataset.saveRecommendation||saveRecommendation.dataset.saveOpenalexRecommendation})});await refresh();toast(result.created?'已加入待读':'这篇论文已在文献库中');}catch(err){saveRecommendation.disabled=false;toast(`保存推荐失败：${err.message}`,'error');}return;}
 const statusBtn=e.target.closest('[data-status-paper]');if(statusBtn){try{await api(`/api/papers/${statusBtn.dataset.statusPaper}`,{method:'PATCH',body:jsonBody({status:statusBtn.dataset.status})});await refresh();toast(`已移入${STATUS[statusBtn.dataset.status]}`);}catch(err){toast(err.message,'error');}return;}
 const decision=e.target.closest('[data-query-decision]');if(decision){try{await api(`/api/queries/${decision.dataset.queryDecision}/matches/${decision.dataset.queryPaper}`,{method:'PATCH',body:jsonBody({user_decision:decision.dataset.decision,viewed:true})});const paper=discoveryResults.find(p=>p.id===decision.dataset.queryPaper);if(paper){paper.user_decision=decision.dataset.decision;paper.seen_before=true;paper.suppressed=decision.dataset.decision==='irrelevant';}renderView();toast(decision.dataset.decision==='irrelevant'?'已记住：此主题下不再重复推荐':'已记住：该论文与你的主题相关');}catch(err){toast(err.message,'error');}return;}
 const recommendation=e.target.closest('[data-recommendation-feedback]');if(recommendation){try{await api(`/api/papers/${recommendation.dataset.recommendationFeedback}/feedback`,{method:'POST',body:jsonBody({decision:recommendation.dataset.decision,scope:'global'})});await refresh();toast('已更新研究画像，后续会减少相似推荐');}catch(err){toast(err.message,'error');}return;}
 const modelTest=e.target.closest('[data-model-test]');if(modelTest){const deep=modelTest.dataset.modelTest==='deep';modelTest.disabled=true;modelTest.textContent=deep?'首 token 测试中…':'测试中…';try{const form=$('#model-settings-form');if(!form.reportValidity())return;const f=new FormData(form);const value=Object.fromEntries(f.entries());value.enabled=f.get('enabled')==='on';value.persist_api_key=f.get('persist_api_key')==='on';value.clear_api_key=f.get('clear_api_key')==='on';await api('/api/settings/model',{method:'PATCH',body:jsonBody(value)});const result=await api('/api/settings/model/test',{method:'POST',body:jsonBody({deep})});const probeTime=result.duration_ms?` · 总耗时 ${Math.round(result.duration_ms)} ms`:'';const firstToken=result.time_to_first_token_ms?` · 首 token ${Math.round(result.time_to_first_token_ms)} ms`:'';const probeKind=result.tested==='models'?'服务连接':'推理接口';const probeNote=result.tested==='inference'&&result.probe_mode==='first_token'?'（首 token 探测成功；为节省时间，已主动结束连接，未等待完整回答）':result.tested==='inference'&&result.stream?'（流式响应已开始）':'';$('#model-config-status').innerHTML=`<span style="color:#60866b">${probeKind}测试成功${probeNote}：${esc(result.model)} · ${esc(result.inference_url||result.base_url)}${firstToken}${probeTime}</span>`;}catch(err){$('#model-config-status').innerHTML=`<span style="color:#b75a53">${esc(err.message)}</span>`;}finally{modelTest.disabled=false;modelTest.textContent=deep?'首 token 实测':'快速连通';}return;}
 const savePaper=e.target.closest('[data-save-paper]');if(savePaper){const id=savePaper.dataset.savePaper;try{await api(`/api/papers/${id}`,{method:'PATCH',body:jsonBody({status:$('#detail-status')?.value,topic:$('#detail-topic')?.value||'',notes:$('#detail-notes')?.value||''})});await refresh();toast('阅读记录已保存');if($('#modal-root').innerHTML)await openPaper(id);}catch(err){toast(err.message,'error');}return;}
 const saveNote=e.target.closest('[data-save-note]');if(saveNote){try{await api(`/api/papers/${saveNote.dataset.saveNote}`,{method:'PATCH',body:jsonBody({notes:$('#reader-note')?.value||''})});await refresh();toast('阅读笔记已保存');}catch(err){toast(err.message,'error');}return;}
 const historicalAi=e.target.closest('[data-show-ai-analysis]');if(historicalAi){const item=aiAnalyses[historicalAi.dataset.showAiAnalysis];if(!item){toast('这条 AI 分析历史已不可用','error');return;}if(item.artifact_type==='abstract_brief'){renderAbstractBrief(item);}else if(item.artifact_type==='reading_card_draft'){aiDrafts[$('.full-ai-analysis [data-ai-reading-card]')?.dataset.aiReadingCard||'']={...item,draft:item.analysis?.reading_card||item.analysis};renderAiReadingCard({...item,draft:item.analysis?.reading_card||item.analysis},$('#ai-reading-result'));}else if(item.artifact_type==='prereading_guide'){const guide=item.analysis?.prereading_guide||item.analysis||{};const result=$('#ai-prereading-result');if(result)result.innerHTML=`<div class="ai-meta">读前准备 · ${esc(item.page_count||'')} 页 · ${esc(item.model||'模型')} · ${esc(fmtDate(item.created_at,true))}</div>${item.analysis_status==='legacy_unverified'?`<div class="ai-quality-warning">${esc(item.analysis_warning||'此历史读前准备尚未通过当前校验。')}</div>`:''}${renderPrereadingGuide(guide)}`;}else renderAiDeepRead(item,$('#ai-deep-result'));return;}
 const runAgent=e.target.closest('[data-agent-brief]');if(runAgent){await requestAgentBrief(runAgent.dataset.agentBrief);return;}
 const prereading=e.target.closest('[data-ai-prereading]');if(prereading){await requestAiPrereadingGuide(prereading.dataset.aiPrereading);return;}
 const autoCard=e.target.closest('[data-ai-reading-card]');if(autoCard){await requestAiReadingCard(autoCard.dataset.aiReadingCard);return;}
 const deepRead=e.target.closest('[data-ai-deep-read]');if(deepRead){await requestAiDeepRead(deepRead.dataset.aiDeepRead);return;}
 const retryJob=e.target.closest('[data-retry-ai-job]');if(retryJob){retryJob.disabled=true;try{const data=await api(`/api/ai-jobs/${retryJob.dataset.retryAiJob}/retry`,{method:'POST',body:'{}'});if(data.job){renderAiJobStatus(data.job);watchAiJob(data.job.paper_id,data.job.id);}toast(data.job?.options?.synthesis_only_artifact_id?'正在仅用已保存证据重新汇总':'已重新加入 AI 任务队列');}catch(err){retryJob.disabled=false;toast(err.message,'error');}return;}
 const cancelJob=e.target.closest('[data-cancel-ai-job]');if(cancelJob){cancelJob.disabled=true;try{const job=await api(`/api/ai-jobs/${cancelJob.dataset.cancelAiJob}/cancel`,{method:'POST',body:'{}'});renderAiJobStatus(job);toast('已请求取消任务');}catch(err){toast(err.message,'error');}return;}
 const applyAiCard=e.target.closest('[data-apply-ai-card]');if(applyAiCard){const id=$('.full-ai-analysis [data-ai-reading-card]')?.dataset.aiReadingCard;if(!id){toast('无法识别当前论文','error');return;}try{const result=await api(`/api/papers/${id}/apply-ai-reading-card`,{method:'POST',body:jsonBody({artifact_id:applyAiCard.dataset.applyAiCard,merge_empty:true})});const paper=papers.find(item=>item.id===id);if(paper)paper.reading_card=result.reading_card;toast('AI 阅读卡已应用：只填充空字段');await openPaper(id);}catch(err){toast(err.message,'error');}return;}
 const syncRefs=e.target.closest('[data-sync-references]');if(syncRefs){const id=syncRefs.dataset.syncReferences;syncRefs.disabled=true;syncRefs.textContent='同步中…';try{const result=await api(`/api/papers/${id}/references/sync`,{method:'POST',body:'{}'});await loadReferences(id);toast(`已保存 ${result.count} 条 OpenAlex 参考文献线索`);}catch(err){toast(err.message,'error');}finally{syncRefs.disabled=false;syncRefs.textContent='同步参考文献';}return;}
 const extract=e.target.closest('[data-extract-paper]');if(extract){const id=extract.dataset.extractPaper;extract.disabled=true;extract.textContent='提取中…';try{const result=await api(`/api/papers/${id}/extract`,{method:'POST',body:'{}'});toast(`已提取 ${result.page_count} 页原文文本`);await loadExtraction(id);}catch(err){toast(err.message,'error');}finally{const current=$(`[data-extract-paper="${CSS.escape(id)}"]`);if(current){current.disabled=false;current.textContent='提取 / 重新提取';}}return;}
 const expand=e.target.closest('[data-expand-paper]');if(expand){await expandPaper(expand.dataset.expandPaper);return;}
 const exp=e.target.closest('[data-export]');if(exp){window.location.href=exp.dataset.export==='bibtex'?'/api/export/bibtex':'/api/export/json';return;}
});

document.addEventListener('click',async e=>{
  const filter=e.target.closest('[data-discovery-filter]');
  if(filter){discoveryTriageFilter=filter.dataset.discoveryFilter||'recommended';persistUiState();renderView();return;}
  const startTriage=e.target.closest('[data-discovery-triage]');
  if(startTriage){await startDiscoveryTriage(true);return;}
  const feedback=e.target.closest('[data-discovery-feedback]');
  if(feedback){await submitDiscoveryFeedback(feedback.dataset.discoveryFeedback,feedback.dataset.discoveryDecision,feedback);return;}
  const retryScreen=e.target.closest('[data-retry-discovery-screen]');
  if(retryScreen){await startDiscoveryTriage(true);return;}
  const refreshHistory=e.target.closest('[data-refresh-discovery-history]');
  if(refreshHistory){refreshHistory.disabled=true;await loadDiscoveryHistory(true);if(refreshHistory.isConnected)refreshHistory.disabled=false;return;}
  const historyPage=e.target.closest('[data-history-page]');
  if(historyPage){discoveryHistoryOffset=Math.max(0,discoveryHistoryOffset+(historyPage.dataset.historyPage==='next'?20:-20));await loadDiscoveryHistory(true);return;}
  const openHistory=e.target.closest('[data-discovery-history-run]');
  if(openHistory){await openDiscoveryHistory(openHistory.dataset.discoveryHistoryRun);return;}
  const rerunHistory=e.target.closest('[data-discovery-history-rerun]');
  if(rerunHistory){await rerunDiscoveryHistory(rerunHistory.dataset.discoveryHistoryRerun,rerunHistory);return;}
  const importOne=e.target.closest('[data-import-candidate]');
  if(importOne){importOne.disabled=true;await importCandidates([importOne.dataset.importCandidate]);return;}
  if(e.target.closest('[data-import-selected-candidates]')){await importCandidates([...selectedCandidateIds]);return;}
  const download=e.target.closest('[data-download-pdf]');
  if(download){await downloadPdf(download.dataset.downloadPdf,download);return;}
  const button=e.target.closest('[data-action="agent-search-plan"]');
  if(!button)return;
  const form=$('#discover-form');
  const query=String(form?.querySelector('[name="query"]')?.value||'').trim();
  const sources=$$('.search-options input[name="source"]:checked').map(input=>input.value);
  if(!query||!sources.length){toast('请先填写研究问题并选择来源','error');return;}
  button.disabled=true;button.textContent='AI 规划并搜索中…';
  try{
    const options={author:form.querySelector('[name="author"]')?.value,venue:form.querySelector('[name="venue"]')?.value,exclude_terms:form.querySelector('[name="exclude_terms"]')?.value,year_from:form.querySelector('[name="year_from"]')?.value,year_to:form.querySelector('[name="year_to"]')?.value,work_type:form.querySelector('[name="work_type"]')?.value,sort:form.querySelector('[name="sort"]')?.value,search_depth:form.querySelector('[name="search_depth"]')?.value,discovery_mode:form.querySelector('[name="discovery_mode"]')?.value,expand_concepts:form.querySelector('[name="expand_concepts"]')?.checked!==false,expand_relations:form.querySelector('[name="expand_relations"]')?.checked===true,suppress_seen:form.querySelector('[name="suppress_seen"]')?.checked===true,ai_screen:form.querySelector('[name="ai_screen"]')?.checked!==false};
    options.fill_repeats=true;
    const plan=await api('/api/discover/agent-plan',{method:'POST',body:jsonBody({query,sources,options})});
    if(!plan.ai_expansion?.enabled)throw new Error(`AI 扩展搜索未启动：${plan.ai_expansion?.reason||'模型没有返回有效检索计划'}`);
    // The plan is an execution contract: immediately send it to the normal
    // multi-source search worker so this action produces papers, not only UI
    // suggestions. Results remain candidates until the user imports them.
    await beginDiscoverySearch(query,sources,options,plan);
    toast(`AI 已生成 ${plan.ai_expansion?.added||0} 条扩展表达，并开始执行多源论文搜索`);
  }catch(err){discoveryMeta={...(discoveryMeta||{}),status:'failed',error:err.message};renderView();toast(err.message,'error');}finally{const current=$('[data-action="agent-search-plan"]');if(current){current.disabled=discoveryStatusActive();current.textContent=discoveryStatusActive()?'✦ AI 搜索中…':'✦ AI 扩展搜索';}}
});

function updateDiscoverySelectionUi(){
  const selected=selectedCandidateIds.size;
  const count=$('#discovery-selection-count');if(count)count.textContent=selected?`(${selected})`:'';
  const action=$('[data-import-selected-candidates]');if(action)action.disabled=!selected;
}
document.addEventListener('change',e=>{
  if(e.target.matches('[data-candidate-check]')){if(e.target.checked)selectedCandidateIds.add(e.target.dataset.candidateCheck);else selectedCandidateIds.delete(e.target.dataset.candidateCheck);persistUiState();updateDiscoverySelectionUi();}
 if(e.target.id==='semantic-index-consent'){const button=$('[data-semantic-index]');if(button)button.disabled=!e.target.checked||e.target.dataset.indexAllowed!=='true';}
 if(e.target.matches('[data-search-backend]')){const controls=e.target.closest('.discovery-backend-controls');const hint=controls?.nextElementSibling;if(hint?.classList.contains('discovery-search-help'))hint.textContent=e.target.value==='model_web'?'模型会拆解研究问题、并行调用学术数据库，必要时使用已配置的 Tavily 或 DeepSeek Search；网页线索会先做 DOI/arXiv/Crossref/OpenAlex 核验，未核验结果只作线索。重复检索优先展示新论文，数量不足时透明补充历史候选；勾选“只看未重复”才会严格隐藏历史结果。':'学术数据库模式按所选来源检索；AI 扩展检索词和相关性初筛可辅助召回与筛选。';const sourceHint=controls?.closest('form')?.querySelector('.discovery-source-preference-help');if(sourceHint)sourceHint.textContent=e.target.value==='model_web'?'优先来源：AI 会根据研究问题选择工具，也可在需要时扩展到其他学术来源。':'检索来源：选择本次检索要查询的数据库。';}
  if(e.target.closest('#discover-form'))captureDiscoveryDraft();
});
document.addEventListener('input',e=>{
 if(e.target.closest('#discover-form'))captureDiscoveryDraft();
 if(e.target.id==='discovery-history-search'){discoveryHistorySearch=e.target.value;discoveryHistoryOffset=0;clearTimeout(discoveryHistorySearchTimer);discoveryHistorySearchTimer=setTimeout(()=>loadDiscoveryHistory(true),400);}
});

document.addEventListener('submit',async e=>{
 if(e.target.id==='agent-goal-form'){e.preventDefault();await submitAgentGoal(e.target);return;}
 if(e.target.matches('[data-extraction-search]')){e.preventDefault();const id=e.target.dataset.extractionSearch,form=new FormData(e.target);const query=String(form.get('q')||'').trim();if(!query){toast('请输入原文检索词','error');return;}await searchDocumentPages(id,query,form.get('semantic')==='on');return;}
 if(e.target.id==='model-settings-form'){e.preventDefault();const f=new FormData(e.target);const value=Object.fromEntries(f.entries());value.enabled=f.get('enabled')==='on';value.persist_api_key=f.get('persist_api_key')==='on';value.clear_api_key=f.get('clear_api_key')==='on';try{const saved=await api('/api/settings/model',{method:'PATCH',body:jsonBody(value)});agentConfigured=saved.api_key_set&&saved.enabled;settingsSnapshot=null;closeModal();toast(saved.api_key_set?`模型配置已保存 · ${saved.api_key_source||'已配置'}`:'模型参数已保存，尚未配置 API Key');}catch(err){$('#model-config-status').innerHTML=`<span style="color:#b75a53">保存失败：${esc(err.message)}</span>`;}return;}
 if(e.target.id==='add-paper-form'){e.preventDefault();const f=new FormData(e.target);const p=Object.fromEntries(f.entries());p.tags=p.tags||'';try{const result=await api('/api/papers',{method:'POST',body:jsonBody(p)});closeModal();await refresh();toast(result.created?'论文已加入文献库':'识别为已有记录，来源已合并');if(result.created)await openPaper(result.id);}catch(err){toast(err.message,'error');}return;}
 if(e.target.id==='add-query-form'){e.preventDefault();const f=new FormData(e.target);const sources=f.getAll('source');const search_options={author:f.get('author'),venue:f.get('venue'),exclude_terms:f.get('exclude_terms'),year_from:f.get('year_from'),year_to:f.get('year_to'),work_type:f.get('work_type'),sort:f.get('sort'),search_depth:f.get('search_depth'),discovery_mode:f.get('discovery_mode'),expand_concepts:f.get('expand_concepts')==='on',expand_relations:f.get('expand_relations')==='on',suppress_seen:f.get('suppress_seen')==='on',fill_repeats:true};try{await api('/api/queries',{method:'POST',body:jsonBody({name:f.get('name'),query:f.get('query'),sources,search_options,cadence_hours:Number(f.get('cadence'))})});closeModal();await refresh();setView('automation');toast('主题已保存，首次同步正在后台启动');}catch(err){toast(err.message,'error');}return;}
 if(e.target.id==='discover-form'){
  e.preventDefault();captureDiscoveryDraft();
  const query=String(discoveryDraft.query||'').trim(), sources=discoveryDraft.sources||[], options={...DISCOVERY_DEFAULT_OPTIONS,...discoveryDraft.options};
  if(!query||(options.search_backend!=='model_web'&&!sources.length)){toast(options.search_backend==='model_web'?'请填写研究问题':'学术数据库模式还需选择至少一个来源','error');return;}
  const runId=newRequestId();activeView='discover';activeDiscoveryRunId=runId;discoveryDraftRunId=runId;
  selectedCandidateIds.clear();stopDiscoveryTriageWatch();discoveryTriageFilter='recommended';discoveryResults=[];discoveryQuery=query;
  discoveryMeta={id:runId,status:'queued',query,query_name:query,source_names:sources,papers:[],candidate_mode:true,plan:{search_options:options}};
  persistUiState();renderSidebar();renderView();
  try{
   const result=await api('/api/discover',{method:'POST',body:jsonBody({query,sources,limit:25,options,request_id:runId,ai_screen:options.ai_screen})});
   if(activeDiscoveryRunId!==runId){loadDiscoveryHistory(true);return;}
   discoveryMeta={...discoveryMeta,...result};persistUiState();renderView();watchDiscoveryRun(runId);loadDiscoveryHistory(true);
   toast(options.ai_screen?'后台检索中，候选完成后自动进行 AI 初筛':'后台检索中，候选由你选择后入库');
  }catch(error){if(activeDiscoveryRunId===runId){discoveryMeta={...(discoveryMeta||{}),status:'failed',error:error.message};renderView();}loadDiscoveryHistory(true);toast(error.message,'error');}
 }

});

document.addEventListener('change',async e=>{
 if(e.target.id==='model-provider-preset'){const item=modelProviderCatalog.find(value=>value.id===e.target.value);const form=$('#model-settings-form');if(item&&form){form.elements.provider.value=item.name||'';form.elements.website_url.value=item.website_url||'';form.elements.base_url.value=item.base_url||'';form.elements.api_url.value=item.api_url||'';form.elements.protocol.value=item.protocol||'chat_completions';form.elements.model.value=item.default_model||item.models?.[0]||'';syncModelApiKeyRequirement(form);const status=$('#model-config-status');if(status)status.textContent=`已填入 ${item.name} 预设，请输入该服务的 API Key 后保存。`; }return;}
 if(e.target.name==='clear_api_key'){syncModelApiKeyRequirement($('#model-settings-form'));return;}
 if(e.target.id==='library-status'){libraryStatus=e.target.value;loadLibraryPage(true);return;}
 if(e.target.id==='library-topic'){libraryTopic=e.target.value;loadLibraryPage(true);return;}
 if(e.target.id==='library-sort'){librarySort=e.target.value;loadLibraryPage(true);return;}
 if(e.target.id==='library-favorite'){libraryFavorite=e.target.checked;loadLibraryPage(true);return;}
 if(e.target.id==='library-has-card'){libraryHasCard=e.target.checked;loadLibraryPage(true);return;}
 if(e.target.matches('.batch-status')){if(e.target.value)await updateSelectedPapers({status:e.target.value});return;}
 if(e.target.matches('[data-paper-status]')){try{await api(`/api/papers/${e.target.dataset.paperStatus}`,{method:'PATCH',body:jsonBody({status:e.target.value})});await refresh();selectedPaperId=e.target.dataset.paperStatus;renderView();toast('阅读状态已更新');}catch(err){toast(err.message,'error');}return;}
 if(e.target.id==='deepread-queue-filter'){deepReadState.queueFilter=e.target.value||'all';persistUiState();renderView();return;}
 if(e.target.matches('[data-paragraph-page]')){const page=Number(e.target.value||0);const index=(deepReadState.segments||[]).findIndex(item=>item.page===page);if(index>=0){deepReadState.segmentIndex=index;persistUiState();renderView();}return;}
 if(e.target.matches('[data-select-paper]')){selectedPaperId=e.target.dataset.selectPaper;renderView();return;}
 if(e.target.matches('[data-attach-paper]')){await attachPdf(e.target.dataset.attachPaper,e.target.files?.[0]);return;}
 if(e.target.matches('[data-download-pdf]')){await downloadPdf(e.target.dataset.downloadPdf,e.target);return;}
 if(e.target.id==='import-file'&&e.target.files?.[0]){await importFile(e.target.files[0]);return;}
});

document.addEventListener('click',async e=>{
 const action=e.target.closest('[data-action]')?.dataset.action;
 if(action==='pdf-import'){pdfImportModal();return;}
 if(action==='paper-metadata-ai'){await completeManualPaperMetadata(e.target.closest('[data-action]'));return;}
 const metadataRetry=e.target.closest('[data-retry-metadata-draft]');if(metadataRetry){metadataRetry.disabled=true;try{const result=await api(`/api/metadata-draft-jobs/${encodeURIComponent(metadataRetry.dataset.retryMetadataDraft)}/retry`,{method:'POST',body:'{}'});metadataDraftJob=result.job;localStorage.setItem(METADATA_DRAFT_JOB_KEY,result.job.id);renderMetadataDraftJob(result.job);renderGlobalTaskStatus();watchMetadataDraftJob(result.job.id);toast(result.deduplicated?'元数据识别已在运行或已有结果':'已重新加入元数据识别队列');}catch(error){metadataRetry.disabled=false;toast(error.message,'error');}return;}
 if(action==='pdf-folder-scan'){await scanPdfFolder();return;}
 const retry=e.target.closest('[data-pdf-retry]');if(retry){retry.disabled=true;try{await api(`/api/pdf-ingest/${encodeURIComponent(retry.dataset.pdfRetry)}/retry`,{method:'POST',body:'{}'});await loadPdfIngestJobs(true);startPdfIngestPolling();toast('失败项已重新加入队列');}catch(error){toast(error.message,'error');}finally{if(retry.isConnected)retry.disabled=false;}return;}
 const pause=e.target.closest('[data-pdf-folder-disable]');if(pause){pause.disabled=true;try{await api(`/api/pdf-folders/${encodeURIComponent(pause.dataset.pdfFolderDisable)}`,{method:'DELETE',body:'{}'});await loadPdfIngestJobs(true);toast('文件夹自动监控已暂停');}catch(error){toast(error.message,'error');}return;}
});
document.addEventListener('submit',async e=>{
 if(e.target.id==='pdf-upload-form'){e.preventDefault();await uploadPdfBatch(e.target);}
});

let libraryTimer=null;
document.addEventListener('input',e=>{
 if(e.target.name==='provider'){syncModelApiKeyRequirement($('#model-settings-form'));}
 if(e.target.id==='library-filter'){libraryQuery=e.target.value;clearTimeout(libraryTimer);libraryTimer=setTimeout(()=>loadLibraryPage(true,true),240);}
 if(e.target.id==='deepread-queue-search'){deepReadState.queueQuery=e.target.value;persistUiState();clearTimeout(deepReadState.queueTimer);deepReadState.queueTimer=setTimeout(()=>{if(activeView==='deepread')renderView();},220);}
 if(e.target.id==='global-search'){
  const value=e.target.value,query=value.trim()?value:'';
  if(query&&activeView!=='library'){activeView='library';persistUiState();$$('.nav-item[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view==='library'));$('#crumb-current').textContent='文献库';renderView();}
  if(query||activeView==='library'){
   libraryQuery=query;clearTimeout(libraryTimer);libraryTimer=setTimeout(()=>loadLibraryPage(true),240);
  }
 }
});
$('#global-search').addEventListener('keydown',e=>{if(e.key==='Enter'){clearTimeout(libraryTimer);libraryQuery=e.target.value.trim()?e.target.value:'';setView('library');}});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal();if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();$('#global-search').focus();}});
$('#add-topic-shortcut').addEventListener('click',addQueryModal);
$('#backup-shortcut').addEventListener('click',createBackup);
$('#help-button').addEventListener('click',()=>toast(`本地资料目录：${appState?.data_dir||'正在读取'}`));

async function runSavedQuery(id){const btn=$(`[data-run-query="${CSS.escape(id)}"]`);if(btn){btn.disabled=true;btn.textContent='同步中…';}try{const r=await api(`/api/queries/${id}/run`,{method:'POST',body:'{}'});selectedCandidateIds.clear();stopDiscoveryTriageWatch();activeDiscoveryRunId=r.id;discoveryQuery=r.query||r.query_name||'';discoveryResults=r.papers||[];discoveryMeta={...r,status:r.status||'queued',candidate_mode:true};restoreDiscoveryDraft(r,true);activeView='discover';persistUiState();await refresh();renderView();watchDiscoveryRun(r.id);toast('主题检索已加入后台；候选会显示在发现页，由你选择后再加入文献库');}catch(e){toast(e.message,'error');}finally{if(btn){btn.disabled=false;btn.textContent='立即运行';}}}
async function attachPdf(id,file){if(!file)return;if(file.size>100*1024*1024){toast('PDF 超过 100 MB 限制','error');return;}const reader=new FileReader();reader.onload=async()=>{try{const b64=String(reader.result).split(',')[1];await api(`/api/papers/${id}/attachment`,{method:'POST',body:jsonBody({filename:file.name,data:b64})});await refresh();toast('PDF 已保存到本机资料库');if($('#modal-root').innerHTML)await openPaper(id);else{selectedPaperId=id;setView('reading');}}catch(e){toast(e.message,'error');}};reader.readAsDataURL(file);}
async function downloadPdf(id,button){if(!button)return;button.disabled=true;button.textContent='查找中…';try{const result=await api(`/api/papers/${id}/download-pdf`,{method:'POST',body:'{}'});await refresh();toast(result.deduplicated?'本地已存在该 PDF':'PDF 已自动下载并保存');await openPaper(id);}catch(e){toast(e.message,'error');}finally{if(button.isConnected){button.disabled=false;button.textContent='查找并下载 PDF';}}}
async function importCandidates(ids){
 const runId=activeDiscoveryRunId, selected=(ids||[]).filter(Boolean);if(!selected.length){toast('请先选择要加入文献库的候选论文','error');return;}
 try{
  const result=await api(`/api/runs/${encodeURIComponent(runId)}/import-candidates`,{method:'POST',body:jsonBody({candidate_ids:selected})});
  if(activeDiscoveryRunId===runId){const map=result.candidate_map||{};discoveryResults=discoveryResults.map(item=>map[item.id]?{...item,imported_paper_id:map[item.id],status:'inbox'}:item);selected.forEach(id=>selectedCandidateIds.delete(id));persistUiState();}
  await refresh();toast(`已加入 ${result.imported||0} 篇论文`);loadDiscoveryHistory(true);if(activeView==='discover')renderView();
 }catch(error){toast(error.message,'error');if(activeView==='discover')renderView();}
}
async function importFile(file){const status=$('#import-status');status.textContent='正在读取并导入…';try{const content=await file.text();const ext=file.name.toLowerCase().split('.').pop();const format=ext==='bib'||ext==='bibtex'?'bibtex':ext==='ris'?'ris':'json';const result=await api('/api/import',{method:'POST',body:jsonBody({format,content})});status.innerHTML=`<span style="color:#60866b">完成：新增 ${result.imported} 篇 · 去重 ${result.duplicates} 篇 · 跳过错误 ${result.errors} 条</span>`;await refresh();toast('文献导入完成');}catch(e){status.innerHTML=`<span style="color:#b75a53">导入失败：${esc(e.message)}</span>`;}}
function pdfIngestStage(stage){return ({queued:'排队中',reading:'读取 PDF 文字',storing:'保存附件',metadata:'提取全文分页',ai_metadata:'AI 补全元数据',completed:'完成',failed:'失败'})[stage]||stage||'处理中';}
function pdfIngestStatus(status){return ({queued:'排队中',running:'处理中',success:'完成',partial:'部分失败',failed:'失败'})[status]||status||'未知';}
function renderPdfIngestJobs(){
 const target=$('#pdf-ingest-jobs');if(!target)return;
 const jobs=pdfIngestJobs||[];
 target.innerHTML=jobs.length?jobs.slice(0,8).map(job=>{
  const settled=Number(job.completed||0)+Number(job.failed||0),pct=job.total?Math.round(settled/Number(job.total)*100):0;
  const retryable=(job.items||[]).some(item=>item.status==='failed'||item.ai_status==='failed');
  const items=(job.items||[]).map(item=>{const result=item.status==='success'?(item.ai_status==='failed'?'已入库 · AI补全失败':item.ai_status==='completed'?'已入库 · AI补全完成':item.duplicate?'重复 PDF · 已复用附件':'已入库 · AI未调用'):item.status==='failed'?'失败':pdfIngestStage(item.stage);return `<div class="pdf-ingest-item"><span class="pdf-ingest-led ${esc(item.status)}"></span><span class="pdf-ingest-name" title="${esc(item.filename)}">${esc(item.filename)}</span><span class="pdf-ingest-item-state">${esc(result)}${item.page_count?` · ${Number(item.page_count)} 页`:''}${item.metadata_source?` · ${esc(item.metadata_source)}`:''}</span>${item.error?`<small class="pdf-ingest-error">${esc(item.error)}</small>`:''}</div>`;}).join('');
  return `<div class="pdf-ingest-job"><div class="pdf-ingest-job-head"><b>${esc(pdfIngestStatus(job.status))} · 已处理 ${settled}/${Number(job.total||0)}</b><span>${Number(job.failed||0)?`${Number(job.failed)} 失败 · `:''}${Number(job.duplicates||0)} 重复${Number(job.ai_completed||0)?` · AI 补全 ${Number(job.ai_completed)}`:''}${Number(job.ai_failed||0)?` · AI 失败 ${Number(job.ai_failed)}`:''}</span></div><div class="ai-job-progress"><i style="width:${pct}%"></i></div><div class="pdf-ingest-job-items">${items}</div>${retryable?`<button class="button small" data-pdf-retry="${esc(job.id)}">重试失败项</button>`:''}</div>`;
 }).join(''):'<div class="pdf-ingest-empty">还没有 PDF 导入任务。</div>';
 const folderTarget=$('#pdf-folder-list');
 if(folderTarget)folderTarget.innerHTML=(pdfFolders||[]).length?pdfFolders.map(folder=>`<div class="pdf-folder-row"><span><b>${esc(folder.path)}</b><small>${folder.enabled?'自动监控中':'已暂停'} · ${folder.recursive?'含子文件夹':'仅当前文件夹'} · ${folder.last_scan_at?`上次扫描 ${esc(fmtDate(folder.last_scan_at,true))}`:'等待首次扫描'}${folder.last_error?` · ${esc(folder.last_error)}`:''}</small></span>${folder.enabled?`<button class="button small" data-pdf-folder-disable="${esc(folder.id)}">暂停</button>`:''}</div>`).join(''):'<div class="pdf-ingest-empty">尚未配置本地监控文件夹。</div>';
}
async function loadPdfIngestJobs(render=true){
 try{const data=await api('/api/pdf-ingest');pdfIngestJobs=data.jobs||[];pdfFolders=data.folders||[];if(render)renderPdfIngestJobs();renderGlobalTaskStatus();return data;}catch(error){const target=$('#pdf-ingest-jobs');if(target)target.textContent=error.message;return null;}
}
function startPdfIngestPolling(){
 if(pdfImportPollTimer)return;
 pdfImportPollTimer=setInterval(async()=>{await loadPdfIngestJobs(true);if(!(pdfIngestJobs||[]).some(job=>['queued','running'].includes(job.status))){clearInterval(pdfImportPollTimer);pdfImportPollTimer=null;}},2200);
}
async function uploadPdfBatch(form){
 const input=$('#pdf-upload-files'), files=[...(input?.files||[])];if(!files.length){toast('请先选择 PDF 文件','error');return;}
 const total=files.reduce((sum,file)=>sum+file.size,0);if(total>180*1024*1024){toast('本批次超过 180 MB，请分批上传','error');return;}if(files.some(file=>file.size>100*1024*1024)){toast('单篇 PDF 不能超过 100 MB','error');return;}
 const button=$('button[type="submit"]',form);if(button){button.disabled=true;button.textContent='正在上传…';}
 try{
  const body=new FormData();for(const file of files)body.append('files',file,file.name);body.append('auto_ai',$('input[name="auto_ai"]',form)?.checked===true?'true':'false');body.append('topic',$('input[name="topic"]',form)?.value||'');
  const result=await api('/api/pdf-ingest',{method:'POST',body});
  await loadPdfIngestJobs(true);startPdfIngestPolling();toast(`已加入 ${files.length} 篇 PDF，后台正在解析并入库`);
 }catch(error){toast(error.message,'error');}
 finally{if(button?.isConnected){button.disabled=false;button.textContent='上传并自动入库';}}
}
async function scanPdfFolder(){
 const path=$('#pdf-folder-path')?.value.trim();if(!path){toast('请输入 LitLoom 所在电脑上的 PDF 文件夹路径','error');return;}
 const button=$('[data-action="pdf-folder-scan"]');if(button){button.disabled=true;button.textContent='正在登记并扫描…';}
 try{const body={path,recursive:$('#pdf-folder-recursive')?.checked===true,auto_ai:$('#pdf-folder-auto-ai')?.checked===true,enabled:$('#pdf-folder-enabled')?.checked===true,interval_minutes:Number($('#pdf-folder-interval')?.value||30)};const result=await api('/api/pdf-ingest/scan',{method:'POST',body:jsonBody(body)});await loadPdfIngestJobs(true);if(result.job?.id)startPdfIngestPolling();toast(result.job?.id?'文件夹已登记，发现的 PDF 正在入库':result.job?.message||'文件夹已保存，未发现新增 PDF');}
 catch(error){toast(error.message,'error');}
 finally{if(button?.isConnected){button.disabled=false;button.textContent='保存设置并立即扫描';}}
}
async function completeManualPaperMetadata(button){
 const form=$('#add-paper-form'),status=$('#paper-metadata-status');if(!form)return;button.disabled=true;button.textContent='提交中…';if(status)status.textContent='已提交到本地后台；离开页面后任务仍会继续。';
 try{const data=Object.fromEntries(new FormData(form).entries());data.background=true;const response=await api('/api/papers/metadata-draft',{method:'POST',body:jsonBody(data)});const job=response.job;if(!job)throw new Error('服务没有返回任务状态');metadataDraftJob=job;localStorage.setItem(METADATA_DRAFT_JOB_KEY,job.id);renderMetadataDraftJob(job);renderGlobalTaskStatus();if(['success','partial'].includes(job.status))applyMetadataDraftJobResult(job);if(['queued','running'].includes(job.status))watchMetadataDraftJob(job.id);scheduleGlobalAiJobsRefresh();}
 catch(error){if(status)status.textContent=`提交失败：${error.message}`;toast(error.message,'error');}
 finally{if(button.isConnected){button.disabled=false;button.textContent='✦ 识别并填写';}}
}
async function restoreBackup(){const file=$('#restore-file')?.files?.[0];if(!file){toast('请先选择本平台 ZIP 备份','error');return;}if(file.size>260*1024*1024){toast('备份包超过当前 260 MB 浏览器恢复上限；可手动替换本机数据目录','error');return;}if(!confirm('恢复会替换当前文献库，系统会先保存当前数据库副本。确定继续？'))return;try{const content=await file.arrayBuffer();let binary='';const bytes=new Uint8Array(content);for(let i=0;i<bytes.length;i+=0x8000)binary+=String.fromCharCode(...bytes.subarray(i,i+0x8000));const data=btoa(binary);const r=await api('/api/backup/restore',{method:'POST',body:jsonBody({filename:file.name,data})});closeModal();await refresh();toast(`备份恢复完成，附件 ${r.attachments_restored} 个`);}catch(e){toast(e.message,'error');}}
async function requestAgentBrief(id){const consent=$('#agent-consent')?.checked;if(!consent){toast('请先确认这篇论文摘要可以发送到模型服务','error');return;}const result=$('#agent-result'),button=$(`[data-agent-brief="${CSS.escape(id)}"]`);if(result)result.innerHTML='<div class="skeleton" style="height:48px"></div>';if(button){button.disabled=true;button.textContent='提交后台任务…';}try{const data=await api(`/api/papers/${id}/agent-brief`,{method:'POST',body:jsonBody({consent:true,background:true})});if(data.job){if(result)result.innerHTML=`<div class="ai-meta">摘要助读已加入后台任务；离开此页面后仍会继续，结果会保存在论文历史中。</div>`;renderAiJobStatus(data.job);watchAiJob(id,data.job.id);toast(data.deduplicated?'摘要助读任务已在运行':'摘要助读已加入后台队列');return;}if(result)result.innerHTML=`<div class="detail-item"><small>基于 ${esc(data.basis)} · ${esc(data.model)}</small><span style="white-space:pre-wrap;line-height:1.75;display:block">${esc(data.answer)}</span></div>`;}catch(e){if(result)result.innerHTML=`<div class="detail-item"><small>助读暂不可用</small><span>${esc(e.message)}</span></div>`;}finally{if(button?.isConnected){button.disabled=!agentConfigured;button.textContent='生成摘要助读';}}}

async function requestAiPrereadingGuide(id){
 if(!$('#full-ai-consent')?.checked){toast('请先确认全文可以发送到模型服务','error');return;}
 const target=$('#ai-prereading-result'),button=$(`[data-ai-prereading="${CSS.escape(id)}"]`);if(target)target.innerHTML='<div class="skeleton" style="height:56px"></div>';
 if(button){button.disabled=true;button.textContent='正在整理读前知识…';}
 try{const data=await api(`/api/papers/${id}/ai-prereading-guide`,{method:'POST',body:jsonBody({background:true,consent:true,template:'paper_prereading_guide',language:'zh-CN'})});if(data.job){renderAiJobStatus(data.job);watchAiJob(id,data.job.id);toast(data.reused?'已复用相同全文的读前准备':(data.deduplicated?'已有读前准备任务在运行':'读前准备任务已加入后台队列'));return;}const guide=data.analysis?.prereading_guide||data.analysis||{};if(target)target.innerHTML=`<div class="ai-meta">读前准备 · ${esc(data.page_count||'')} 页 · ${esc(data.model||'模型')}${data.duration_ms?` · ${Math.round(Number(data.duration_ms)/1000)} 秒`:''}</div>${renderPrereadingGuide(guide)}`;await loadStudyItems(true);}
 catch(e){if(target)target.innerHTML=`<div class="ai-error">读前准备生成失败：${esc(e.message)}</div>`;}
 finally{if(button){button.disabled=!agentConfigured;button.textContent='只生成读前准备';}}
}

async function requestAiReadingCard(id){
 if(!$('#full-ai-consent')?.checked){toast('请先确认全文可以发送到模型服务','error');return;}
 const target=$('#ai-reading-result'),button=$(`[data-ai-reading-card="${CSS.escape(id)}"]`);if(target)target.innerHTML='<div class="skeleton" style="height:56px"></div>';
 if(button){button.disabled=true;button.textContent='正在提取并分析全文…';}
 try{const data=await api(`/api/papers/${id}/ai-reading-card`,{method:'POST',body:jsonBody({background:true,consent:true,apply:true,merge_empty:true,template:'fulltext_reading_card',language:'zh-CN'})});if(data.job){renderAiJobStatus(data.job);watchAiJob(id,data.job.id);toast(data.reused?'已复用相同全文的历史阅读卡':(data.deduplicated?'已有阅读卡任务在运行':'阅读卡任务已加入后台队列'));return;}aiDrafts[id]=data;renderAiReadingCard(data,target);}
 catch(e){if(target)target.innerHTML=`<div class="ai-error">全文阅读卡生成失败：${esc(e.message)}</div>`;}
 finally{if(button){button.disabled=!agentConfigured;button.textContent='自动生成阅读卡';}}
}
async function requestAiDeepRead(id){
 if(!$('#full-ai-consent')?.checked){toast('请先确认全文可以发送到模型服务','error');return;}
 const target=$('#ai-deep-result'),button=$(`[data-ai-deep-read="${CSS.escape(id)}"]`);if(target)target.innerHTML='<div class="skeleton" style="height:56px"></div>';
 if(button){button.disabled=true;button.textContent='正在逐页精读…';}
 try{const data=await api(`/api/papers/${id}/ai-deep-read`,{method:'POST',body:jsonBody({background:true,consent:true,template:'graduate_deep_read',language:'zh-CN'})});if(data.job){renderAiJobStatus(data.job);watchAiJob(id,data.job.id);toast(data.reused?'已复用相同全文的历史精读':(data.deduplicated?'已有精读任务在运行':'精读任务已加入后台队列'));return;}aiDeepReads[id]=data;renderAiDeepRead(data,target);}
 catch(e){if(target)target.innerHTML=`<div class="ai-error">AI 精读失败：${esc(e.message)}</div>`;}
 finally{if(button){button.disabled=!agentConfigured;button.textContent='开始 AI 精读全文';}}
}

window.refresh=refresh;
document.addEventListener('input',e=>{
 if(e.target.id==='study-filter'){studyQuery=e.target.value;clearTimeout(studySearchTimer);studySearchTimer=setTimeout(()=>loadStudyItems(true,true),300);}
});
document.addEventListener('change',async e=>{
 if(e.target.id==='study-mastery'){studyMastery=e.target.value;await loadStudyItems(true);return;}
 if(e.target.id==='study-type'){studyType=e.target.value;await loadStudyItems(true);return;}
 const select=e.target.closest('[data-study-status]');
 if(select){const item=studyPage.items.find(row=>row.id===select.dataset.studyStatus);if(!item)return;const previous=item.mastery;select.disabled=true;try{await api(`/api/study-items/${encodeURIComponent(item.id)}`,{method:'PATCH',body:jsonBody({mastery:select.value})});item.mastery=select.value;await loadStudyItems(true);toast('学习进度已保存');}catch(err){select.value=previous;toast(`保存学习进度失败：${err.message}`,'error');}finally{select.disabled=false;}return;}
});
document.addEventListener('click',async e=>{
 const dueToggle=e.target.closest('[data-study-due-toggle]');if(dueToggle){studyDueOnly=!studyDueOnly;await loadStudyItems(true);return;}
 const review=e.target.closest('[data-study-review]');if(review){const itemId=review.dataset.studyReview,actions=review.closest('.study-review-actions'),buttons=$$('button',actions);buttons.forEach(button=>button.disabled=true);try{await api(`/api/study-items/${encodeURIComponent(itemId)}`,{method:'PATCH',body:jsonBody({review_rating:review.dataset.reviewRating})});await loadStudyItems(true);toast('复习已记录，下次复习时间已安排');}catch(err){buttons.forEach(button=>button.disabled=false);toast(`复习记录失败：${err.message}`,'error');}return;}
 if(e.target.closest('[data-study-load-more]')){await loadStudyItems(false);return;}
 const remove=e.target.closest('[data-study-delete]');
 if(remove){const item=studyPage.items.find(row=>row.id===remove.dataset.studyDelete);if(!item||!confirm(`从读前知识中移除“${item.label}”？论文和 AI 阅读卡不会受到影响。`))return;remove.disabled=true;try{await api(`/api/study-items/${encodeURIComponent(item.id)}`,{method:'DELETE',body:'{}'});await loadStudyItems(true);toast('读前知识条目已移除');}catch(err){remove.disabled=false;toast(`移除失败：${err.message}`,'error');}return;}
 const saveNote=e.target.closest('[data-save-study-note]');
 if(saveNote){const id=saveNote.dataset.saveStudyNote,note=$(`[data-study-note="${CSS.escape(id)}"]`)?.value||'';saveNote.disabled=true;try{await api(`/api/study-items/${encodeURIComponent(id)}`,{method:'PATCH',body:jsonBody({user_note:note})});const item=studyPage.items.find(row=>row.id===id);if(item)item.user_note=note;toast('个人理解已保存');}catch(err){toast(`保存个人理解失败：${err.message}`,'error');}finally{saveNote.disabled=false;}return;}
});
// The discovery backend selector has another delegated change handler above;
// repair its explanatory copy after that handler runs so the result-count
// guarantee remains visible when users switch search modes.
document.addEventListener('change',e=>{
 if(!e.target.matches('[data-search-backend]'))return;
 setTimeout(()=>{
  const hint=e.target.closest('.discovery-backend-controls')?.nextElementSibling;
  if(hint?.classList.contains('discovery-search-help'))hint.textContent=hint.textContent.replace('勾选“只看未重复”才会严格隐藏历史结果。','勾选后仍保持结果数量：优先新论文，数量不足时补齐历史候选并明确标记。');
 },0);
});
initializeWorkspace();
