// Paper detail, PDF evidence, and AI reading experiences.

const readingAnalysisCache=new Map();

async function openPaper(id){
  if(activeView==='reading'){
    selectedPaperId=id;persistUiState();renderView();
    loadReadingPaperDetail(id,true);loadReadingPaperAnalysis(id,true);
    return;
  }
  activeModalPaperId=id;persistUiState();
 let p;
 // Always load the detail record so persisted AI artifacts are available
 // after closing and reopening the paper dialog.
  // The analysis history is fetched by loadAiAnalysis below.  Ask the detail
  // endpoint for metadata-only artifact rows so reopening a paper does not
  // transfer several large JSON answers before the dialog can render.
  try{p=await api(`/api/papers/${id}?view=summary`);}catch{return;}
 if(!p)return;
 let sources=p.sources||[], attachments=p.attachments||[];
 const statusOpts=STATUS_OPTIONS.map(s=>`<option value="${s}" ${p.status===s?'selected':''}>${STATUS[s]}</option>`).join('');
 const modelLabel=agentConfigured?'模型已配置':modelConfigurationHint();
 openModal(`<div class="modal-head"><div><div class="eyebrow" style="margin-bottom:5px">PAPER RECORD · ${esc((sources[0]&&SOURCE_NAME[sources[0].source])||'MY LIBRARY')}</div><h3>论文记录</h3></div><button class="close-button" data-close-modal>×</button></div><div class="modal-body"><h2 class="paper-detail-title">${esc(p.title)}</h2><div class="detail-meta">${esc([p.authors,p.year,p.venue].filter(Boolean).join(' · ')||'作者与出版信息待补充')}</div><p class="detail-abstract">${esc(p.abstract||'来源暂未提供摘要。可以打开论文原文或稍后补充信息。')}</p><div class="detail-grid"><div class="detail-item"><small>DOI</small><span>${p.doi?`<a href="https://doi.org/${encodeURIComponent(p.doi)}" target="_blank" rel="noopener">${esc(p.doi)} ↗</a>`:'未登记'}</span></div><div class="detail-item"><small>arXiv ID</small><span>${esc(p.arxiv_id||'未登记')}</span></div><div class="detail-item"><small>研究主题</small><span>${esc(p.topic||'尚未分类')}</span></div><div class="detail-item"><small>标签</small><span>${esc(getTags(p).join('、')||'暂无标签')}</span></div></div><div class="form-grid"><div class="form-field"><label>阅读状态</label><select class="field-select" id="detail-status">${statusOpts}</select></div><div class="form-field"><label>研究主题</label><input class="field-input" id="detail-topic" value="${esc(p.topic||'')}" placeholder="如：视频理解"></div><div class="form-field full"><label>个人阅读笔记</label><textarea class="field-area large" id="detail-notes" placeholder="问题、方法、实验、局限与自己的判断">${esc(p.notes||'')}</textarea></div></div><div style="display:flex;gap:8px;align-items:center;margin-top:11px;flex-wrap:wrap"><button class="button small primary" data-save-paper="${id}">保存阅读记录</button>${p.url?`<a class="button small" href="${esc(p.url)}" target="_blank" rel="noopener">打开论文来源 ↗</a>`:''}${p.pdf_url?`<a class="button small" href="${esc(p.pdf_url)}" target="_blank" rel="noopener">打开公开 PDF ↗</a>`:''}<label class="button small" style="cursor:pointer">添加本地 PDF<input type="file" accept="application/pdf,.pdf" data-attach-paper="${id}" hidden></label></div><div class="modal-section-title">来源记录 · ${sources.length}</div>${sources.length?sources.map(s=>`<div class="source-line"><span>${esc(SOURCE_NAME[s.source]||s.source)} ${s.external_id?`· ${esc(s.external_id)}`:''}</span><span>记录于 ${fmtDate(s.fetched_at,true)}</span></div>`).join(''):`<div class="source-line"><span>手动添加</span><span>来源记录保存在本地</span></div>`}<div class="modal-section-title">附件 · ${attachments.length}</div>${attachments.length?attachments.map(a=>`<a class="file-chip" href="/files/${esc(a.stored_name)}" target="_blank" rel="noopener">▧ ${esc(a.original_name)} · SHA-256 ${esc(a.sha256.slice(0,12))}…</a>`).join(' '):`<span style="font-size:9px;color:#a2aaa4">暂未关联本地 PDF</span>`}<div id="model-runtime-label" class="modal-section-title">AI 助读 · ${esc(modelLabel)}</div><div class="agent-banner" style="margin:0"><span class="agent-spark">✦</span><div><b>仅使用题名、摘要和元数据</b><p>基于摘要给出研究问题和精读核验建议；不声称具有页码级全文证据。发送到第三方前需要单独勾选确认。</p></div></div><div id="agent-result" style="margin-top:10px"></div><div style="display:flex;gap:7px;align-items:center;margin-top:9px"><label class="check-label"><input id="agent-consent" type="checkbox"> 我同意将该论文摘要发送到已配置的模型服务</label><button class="button small" data-model-required data-source-ready="true" data-agent-brief="${id}" ${agentConfigured?'':'disabled'} title="${esc(agentConfigured?'生成摘要助读':modelLabel)}">生成摘要助读</button></div></div><div class="modal-foot"><button class="button" data-close-modal>关闭</button><button class="button primary" data-save-paper="${id}">保存更改</button></div>`);
 {const upload=$(`[data-attach-paper="${CSS.escape(id)}"]`);const label=upload?.closest('label');if(label){const button=document.createElement('button');button.className='button small';button.type='button';button.dataset.downloadPdf=id;button.textContent='查找并下载 PDF';button.title='根据 PDF 地址、DOI、arXiv ID 或论文来源自动查找并保存';label.before(button);}}
 // The editable reading card belongs to the Reading Desk. Keeping it out of
 // this metadata modal prevents the paper record from becoming a second
 // reading workspace and gives the card a durable place to resume later.
 loadAbstractBrief(p.ai_artifacts||[]);
 addPaperHistoryButton(id);
 const sourceReady=typeof p.has_fulltext_source==='boolean'?p.has_fulltext_source:Boolean((p.attachments||[]).length);
 const pdfReady=typeof p.has_local_pdf==='boolean'?p.has_local_pdf:sourceReady;
 addFullAiAnalysisControls(id,sourceReady,pdfReady);
 updateModelDependentControls();
}

function loadAbstractBrief(artifacts=[]){
 const item=(Array.isArray(artifacts)?artifacts:[]).find(row=>row.artifact_type==='abstract_brief');
 if(!item)return;
 renderAbstractBrief(item);
}

function renderAbstractBrief(item,target=$('#agent-result')){
 if(!target||!item)return;
 const answer=item.answer||item.analysis?.answer||'';
 target.innerHTML=`<div class="detail-item"><small>最近生成 · ${esc(fmtDate(item.created_at,true))} · ${esc(item.model||item.analysis?.model||'模型')} · 标题、摘要和元数据</small><span style="white-space:pre-wrap;line-height:1.75;display:block">${esc(answer)}</span></div>`;
}

function addPaperHistoryButton(id){const body=$('.modal-body');if(!body||body.querySelector('.paper-history-actions'))return;const section=document.createElement('section');section.className='paper-history-actions';section.innerHTML=`<div class="modal-section-title">长期记录</div><button class="button small" data-paper-history="${id}">查看笔记与阅读卡历史</button>`;body.append(section);}

async function loadReadingCard(id){
 const body=$('.modal-body');if(!body)return;
 try{const card=await api(`/api/papers/${id}/reading-card`);const section=document.createElement('section');section.className='reading-card-section';section.innerHTML=`<div class="modal-section-title">结构化阅读卡 · ${card.completion_pct||0}% 完成</div><div class="form-grid reading-card-grid">${readingCardFields(card)}</div><div style="display:flex;justify-content:flex-end;margin-top:9px"><button class="button small primary" data-save-reading-card="${id}">保存结构化阅读卡</button></div>`;body.insertBefore(section,body.querySelector('.citation-section')||body.lastElementChild);}
 catch(e){toast(`阅读卡读取失败：${e.message}`,'error');}
}

function aiEvidenceRows(items=[], key='quote'){
 return (Array.isArray(items)?items:[]).slice(0,30).map(item=>`<div class="ai-evidence-row"><b>第 ${esc(item.page??'?')} 页</b><span>${esc(item[key]||item.quote||'')}</span><small>${esc(item.support||item.confidence||'待核验')}</small></div>`).join('');
}
function aiVisualRows(items=[]){
  return (Array.isArray(items)?items:[]).slice(0,40).map(item=>`<div class="ai-visual-row"><div class="ai-visual-head"><b>${esc(item.type||'视觉对象')}</b><span>第 ${esc(item.page??'?')} 页 · ${esc(item.confidence||'待核验')}</span></div><p>${esc(item.description||item.claim||'')}</p>${item.transcription_latex?`<code>${esc(item.transcription_latex)}</code>`:''}${item.data_relationships?`<small>关系：${esc(item.data_relationships)}</small>`:''}</div>`).join('');
}
function aiVisualPanel(data, analysis={}){
  const visual=analysis.visual_analysis||{};const evidence=analysis.visual_evidence||analysis.visual_observations||[];
  const groups=[['公式',visual.formulas],['图与曲线',visual.figures],['表格',visual.tables],['流程图/示意图',visual.diagrams]];
  const groupHtml=groups.filter(([,value])=>value&&((Array.isArray(value)&&value.length)||(!Array.isArray(value)&&String(value).trim()))).map(([label,value])=>`<div class="ai-visual-group"><b>${label}</b>${Array.isArray(value)?`<ul>${value.slice(0,20).map(item=>`<li>${esc(typeof item==='string'?item:JSON.stringify(item))}</li>`).join('')}</ul>`:`<p>${esc(value)}</p>`}</div>`).join('');
  const failures=(data.visual_failures||[]).length;const hasVisualFindings=Boolean(groupHtml||(Array.isArray(evidence)&&evidence.length));const rechecks=Number(data.visual_recheck_count||0);const recheckAttempts=Number(data.visual_recheck_attempt_count||0);const unverified=Number(data.unverified_visual_claim_count||0);const observationCount=Number(data.visual_evidence_count??evidence.length);const observationPages=Number(data.visual_evidence_pages??new Set(evidence.map(item=>item?.page).filter(Boolean)).size);const status=data.visual_requested?(data.visual_pages?`图像已发送 ${esc(data.visual_pages)} 页 · 有效观察 ${esc(observationCount)} 条 / ${esc(observationPages)} 页${recheckAttempts?` · 补查 ${esc(recheckAttempts)} 页，命中 ${esc(rechecks)} 页`:''}`:'未成功渲染视觉页'):data.visual_enabled===false?'文本模式 · 未进行图像识别':'未启用视觉页';
  const refs=data.visual_page_refs||[];const refHtml=refs.length?`<div class="ai-visual-page-links">${refs.slice(0,20).map(item=>`<a href="${esc(item.url||item.pdf_url||'#')}" target="_blank" rel="noopener">查看第 ${esc(item.page||'?')} 页图像 ↗</a>`).join('')}</div>`:'';
  const missingFindings=data.visual_requested&&Number(data.visual_pages||0)>0&&!hasVisualFindings;
  return `<div class="ai-visual-panel"><div class="ai-subtitle">公式、图表与图像识别 · ${status}</div>${failures?`<div class="ai-quality-warning">有 ${esc(failures)} 个视觉处理限制；无法确认的公式或图表不会被当作事实，请回看原 PDF。</div>`:''}${unverified?`<div class="ai-quality-warning">${esc(unverified)} 条视觉总结没有对应到已核验的图像观察，已从视觉结果中移除；请回看 PDF 原页确认。</div>`:''}${missingFindings?'<div class="ai-quality-warning">图像页已送入模型，但模型没有返回可核验的视觉观察。下方公式解析不能据此认定为图像识别结果，请回看对应 PDF 页面。</div>':''}${refHtml}${groupHtml?`<div class="ai-visual-grid">${groupHtml}</div>`:''}${evidence.length?`<div class="ai-subtitle">视觉证据锚点</div><div class="ai-visual-list">${aiVisualRows(evidence)}</div>`:''}</div>`;
}
function aiModelCallDetails(data={}){
 const calls=Array.isArray(data.model_calls)?data.model_calls:[];if(!calls.length)return '';
 const names={chunk_analysis:'正文分块',chunk_analysis_fallback:'文本降级',visual_recheck:'视觉补查',final_synthesis:'跨页综合',synthesis_repair:'结构修复'};
 const totalSeconds=calls.reduce((sum,item)=>sum+Number(item.duration_ms||0),0)/1000;
 return `<details class="ai-call-details"><summary>模型阶段明细 · ${calls.length} 次请求，累计 ${Math.round(totalSeconds)} 秒（含并行）</summary><ul>${calls.map((item,index)=>{const pages=Array.isArray(item.pages)?item.pages:[];return `<li><span>${index+1}. ${esc(names[item.phase]||item.phase||'模型请求')}</span><small>${Math.round(Number(item.duration_ms||0)/1000)} 秒${pages.length?` · PDF 第 ${esc(pages.join(', '))} 页`:''} · ${esc(item.input_chars||0)} 字符${item.image_count?` · ${esc(item.image_count)} 页图像`:''}${item.status&&item.status!=='success'?` · ${esc(item.status)}`:''}</small></li>`;}).join('')}</ul></details>`;
}
function aiDeepStructuredPanel(data, analysis={}){
  const problem=analysis.problem_formulation||{};
  const method=analysis.method||{};
  const equation=Array.isArray(analysis.equation_analysis)?analysis.equation_analysis:[];
  const tables=Array.isArray(analysis.result_tables)?analysis.result_tables:[];
  const datasets=Array.isArray(analysis.datasets)?analysis.datasets:[];
  const experiments=Array.isArray(analysis.experiments)?analysis.experiments:[];
  const metrics=Array.isArray(analysis.metrics)?analysis.metrics:[];
  const value=(item,key)=>esc(typeof item==='string'?item:(item?.[key]||JSON.stringify(item)));
  const list=(items,key='description')=>Array.isArray(items)&&items.length?`<ul>${items.map(item=>`<li>${value(item,key)}</li>`).join('')}</ul>`:'';
  const block=(stage,title,body,help='')=>body?`<section class="ai-deep-structured" data-deep-stage="${stage}"><div class="ai-subtitle">${title}</div>${help?`<p class="ai-stage-help">${help}</p>`:''}${body}</section>`:'';
  const problemBody=Object.entries(problem).filter(([,v])=>v!==undefined&&v!==null&&String(v).trim()).map(([key,v])=>`<div class="ai-structured-line"><b>${esc(key)}</b><span>${esc(Array.isArray(v)?v.join('；'):typeof v==='object'?JSON.stringify(v):v)}</span></div>`).join('');
  const methodBody=[method.pipeline?`<div class="ai-structured-line"><b>方法主线</b><span>${esc(method.pipeline)}</span></div>`:'',method.training_or_inference?`<div class="ai-structured-line"><b>训练 / 推理</b><span>${esc(method.training_or_inference)}</span></div>`:'',method.components?.length?`<div class="ai-structured-line ai-structured-stack"><b>组成模块</b>${list(method.components,'name')}</div>`:'',method.assumptions?.length?`<div class="ai-structured-line ai-structured-stack"><b>假设</b>${list(method.assumptions,'description')}</div>`:''].join('');
  const stepsBody=list(method.algorithm_steps);
  const equationBody=equation.length?equation.map(item=>`<div class="ai-equation-row"><b>${esc(item.name||((item.page||item.page===0)?`第 ${item.page||'?'} 页`:'公式'))}</b>${item.latex||item.transcription_latex?`<code>${esc(item.latex||item.transcription_latex)}</code>`:''}<p>${esc(item.explanation||item.role||item.variables||JSON.stringify(item))}</p></div>`).join(''):'';
  const datasetBody=list(datasets,'description')||list(datasets,'name');
  const experimentBody=list(experiments,'description')||list(experiments,'name');
  const tableBody=tables.length?`<div class="ai-result-table-list">${tables.map(item=>`<div class="ai-result-table-row"><b>${esc(item.name||item.title||`第 ${item.page||'?'} 页`)}</b><span>${esc(item.summary||item.comparison||item.data||JSON.stringify(item))}</span></div>`).join('')}</div>`:'';
  const metricBody=list(metrics,'description')||list(metrics,'name');
  return `<div class="ai-deep-structured-panel">${block('method','任务与方法结构',problemBody?`<div class="ai-structured-grid">${problemBody}</div>`:'','先说明论文在解决什么，再展开输入、输出、目标和约束。')}${block('method','方法主线',methodBody||stepsBody?`${methodBody}${stepsBody?`<div class="ai-structured-line ai-structured-stack"><b>执行步骤</b>${stepsBody}</div>`:''}`:'','只放方法、训练/推理和可复现的步骤。')}${block('method','公式与变量',equationBody,'公式来自 PDF 页面或模型视觉观察；没有页码证据的内容会明确标为待核验。')}${block('method','数据与实验设置',datasetBody||experimentBody?`${datasetBody?`<div class="ai-structured-line ai-structured-stack"><b>数据集 / 数据来源</b>${datasetBody}</div>`:''}${experimentBody?`<div class="ai-structured-line ai-structured-stack"><b>实验设置</b>${experimentBody}</div>`:''}`:'','先看数据和对照，再判断结果是否可比较。')}${block('results','结果表格与指标',tableBody||metricBody?`${tableBody}${metricBody?`<div class="ai-structured-line ai-structured-stack"><b>评价指标</b>${metricBody}</div>`:''}`:'','这一栏只展示结果、指标和数值关系，不重复方法说明。')}</div>`;
}
function renderPrereadingGuide(guide={}){
 const prerequisites=Array.isArray(guide?.prerequisites)?guide.prerequisites:[];
 const glossary=Array.isArray(guide?.glossary)?guide.glossary:[];
 const notation=Array.isArray(guide?.notation)?guide.notation:[];
 const checks=Array.isArray(guide?.self_check)?guide.self_check:[];
 const evidence=Array.isArray(guide?.evidence)?guide.evidence:[];
 const explicitRoute=Array.isArray(guide?.learning_route)?guide.learning_route:[];
 const route=explicitRoute.length?explicitRoute:prerequisites.map((item,index)=>({step:index+1,title:item.concept,goal:item.minimum_to_know,why:item.why_needed,estimated_minutes:'',depends_on:''}));
 if(!route.length&&!prerequisites.length&&!glossary.length&&!notation.length&&!checks.length)return '';
 const anchor=(term)=>evidence.find(item=>String(item.term||'').trim().toLowerCase()===String(term||'').trim().toLowerCase());
 const routeRows=route.map((item,index)=>`<article class="prereading-route-row"><div class="prereading-route-number">${esc(item.step||index+1)}</div><div><div class="prereading-route-title"><b>${esc(item.title||item.concept||'学习步骤')}</b>${item.estimated_minutes?`<span>${esc(item.estimated_minutes)} 分钟</span>`:''}</div><p>${esc(item.goal||'掌握理解本文所需的最小知识')}</p>${item.why?`<small>为什么先学：${esc(item.why)}</small>`:''}${item.depends_on?`<small>前置：${esc(item.depends_on)}</small>`:''}</div></article>`).join('');
 const prerequisiteRows=prerequisites.map((item,index)=>{const explicit=item.necessity==='required'&&item.basis==='paper';const tag=explicit?'论文明确依赖':item.basis==='inferred'?'AI建议补充':'建议掌握';return `<div class="prereading-row"><div><span class="prereading-order">${item.necessity==='required'?'必须':'可选'}</span><b>${esc(item.concept||'未命名概念')}</b><span class="prereading-tag">${tag}</span></div><p>${esc(item.why_needed||'')}</p>${item.minimum_to_know?`<small>最低掌握：${esc(item.minimum_to_know)}</small>`:''}</div>`}).join('');
 const glossaryRows=glossary.map(item=>{const ref=anchor(item.term);const source=item.definition_basis==='paper'?'本文定义':'背景释义';return `<div class="prereading-row"><div><b>${esc(item.term||'术语')}${item.expansion?` · ${esc(item.expansion)}`:''}</b><span class="prereading-tag">${source}</span></div><p>${esc(item.plain_definition||'')}</p>${item.role_in_paper?`<small>本文中的作用：${esc(item.role_in_paper)}</small>`:''}${ref?`<blockquote>原文依据 · 第 ${esc(ref.page)} 页：${esc(ref.quote)}</blockquote>`:''}</div>`}).join('');
 const notationRows=notation.map(item=>{const ref=anchor(item.symbol);return `<div class="prereading-row notation"><div><b>${esc(item.symbol||'符号')}</b><span>${esc(item.meaning||'')}</span></div>${item.caveat?`<small>注意：${esc(item.caveat)}</small>`:''}${ref?`<blockquote>原文依据 · 第 ${esc(ref.page)} 页：${esc(ref.quote)}</blockquote>`:''}</div>`}).join('');
 const checkRows=checks.map(item=>`<details class="prereading-check"><summary>${esc(item.question||'自测问题')}</summary><p>${esc(item.key_point||'')}</p></details>`).join('');
 const uncertainties=(guide.uncertainties||[]).map(item=>`<li>${esc(item)}</li>`).join('');
 const counts=`<div class="prereading-guide-stats"><span><b>${route.length}</b>步学习路线</span><span><b>${glossary.length}</b>个术语</span><span><b>${notation.length}</b>个符号</span><span><b>${checks.length}</b>道自测</span></div>`;
 return `<section class="ai-prereading-guide"><div class="ai-prereading-head"><div><b>读前准备 · 先学什么再读什么</b><small>只保留理解本文真正需要的知识；背景释义与论文原文定义分开标注。</small></div><span>可回到 PDF 核对</span></div>${counts}${route.length?`<div class="ai-prereading-section prereading-route"><h4>建议学习顺序</h4>${routeRows}</div>`:''}${prerequisites.length?`<details class="ai-prereading-section prereading-collapsible" open><summary>为什么需要这些知识 <span>${prerequisites.length} 项</span></summary>${prerequisiteRows}</details>`:''}${glossary.length?`<details class="ai-prereading-section prereading-collapsible"><summary>论文术语表 <span>${glossary.length} 项</span></summary>${glossaryRows}</details>`:''}${notation.length?`<details class="ai-prereading-section prereading-collapsible"><summary>符号与公式速查 <span>${notation.length} 项</span></summary>${notationRows}</details>`:''}${checks.length?`<details class="ai-prereading-section prereading-collapsible"><summary>读前自测 <span>${checks.length} 题</span></summary>${checkRows}</details>`:''}${uncertainties?`<div class="prereading-uncertainty-box"><b>仍需回到原文确认</b><ul class="ai-prereading-uncertainties">${uncertainties}</ul></div>`:''}</section>`;
}
function renderAiReadingCard(data, target){
 const draft=data?.draft||data?.analysis?.reading_card||data?.analysis||{};const evidence=data?.analysis?.evidence||data?.evidence||[];
 const legacyUnverified=data?.analysis_status==='legacy_unverified';
  const applied=Boolean(data?.applied||data?.auto_applied);const artifactId=data.artifact_id||data.id||'';
  const timing=data?.duration_ms?` · ${Math.round(Number(data.duration_ms)/1000)} 秒 · ${data.model_call_count||'?'} 次模型请求${data.model_call_average_ms?` · 平均响应 ${Math.round(Number(data.model_call_average_ms)/1000)} 秒，最长 ${Math.round(Number(data.model_call_max_ms||0)/1000)} 秒`:''}`:'';
  target.innerHTML=`<div class="ai-meta">全文阅读卡 · ${esc(data?.page_count||'')} 页 · ${esc(data?.model||'模型')} · ${esc(fmtDate(data?.created_at,true))}${timing} ${applied?'<span class="evaluation-status">已自动填充空字段</span>':''}</div>${legacyUnverified?`<div class="ai-quality-warning">此历史阅读卡未通过当前结构校验，仅供回看；请对照原 PDF 或重新生成。${data?.analysis_warning?`<br>${esc(data.analysis_warning)}`:''}</div>`:''}${data?.low_quality_pages?`<div class="ai-quality-warning">有 ${esc(data.low_quality_pages)} 个低质量或空白页，相关结论可能不完整，请重点核对 PDF。</div>`:''}${!evidence.length?'<div class="ai-quality-warning">阅读卡没有可核验的页码引文；请把它作为阅读提纲，并回到 PDF 确认具体结论。</div>':''}${renderPrereadingGuide(data?.analysis?.prereading_guide)}<div class="ai-card-grid">${Object.entries(READING_CARD_FIELDS).map(([key,label])=>`<div class="ai-card-item"><b>${esc(label)}</b><p>${esc(draft[key]||'原文未说明')}</p></div>`).join('')}</div>${evidence.length?`<div class="ai-subtitle">页码证据</div><div class="ai-evidence-list">${aiEvidenceRows(evidence)}</div>`:''}${aiVisualPanel(data,data?.analysis||{})}<div class="ai-actions">${applied?'<span class="ai-hint">已有人工内容已保留；本次结果只填充原本为空的字段，并已建立历史版本。</span>':`<button class="button small" data-apply-ai-card="${esc(artifactId)}" ${artifactId?'':'disabled'}>应用到阅读卡（仅填充空字段）</button><span class="ai-hint">应用会保留已有人工内容，并创建历史版本。</span>`}</div>`;
}
function renderAiDeepRead(data, target){
 const a=data?.analysis||data||{};const method=a.method||{};const critical=a.critical_reading||{};const list=(value)=>Array.isArray(value)?value.map(item=>`<li>${esc(typeof item==='string'?item:JSON.stringify(item))}</li>`).join(''):(value?`<li>${esc(value)}</li>`:'<li>原文未说明</li>');
 const textEvidence=Array.isArray(a.evidence_spans)?a.evidence_spans:[];
 const partial=a.analysis_status==='partial'||data?.analysis_status==='partial';
 const legacyUnverified=a.analysis_status==='legacy_unverified'||data?.analysis_status==='legacy_unverified';
 const partialFindings=Array.isArray(a.partial_findings)?a.partial_findings:[];const evidenceGap=partial&&a.partial_kind==='evidence_gap';const evidenceAudit=a.evidence_audit||{};
 const partialContent=partial&&!evidenceGap?`<div class="ai-quality-warning"><b>精读部分完成。</b>跨页综合未完成，下面只显示有 PDF 页码证据的分块发现；重试只会综合已保存证据，不会重新读取 PDF 图像。${a.partial_reason?`<br>${esc(a.partial_reason)}`:''}</div><section class="ai-deep-partial"><h4>已核验的分块发现</h4>${partialFindings.length?partialFindings.map(item=>`<article><b>${esc(item.category||'论文发现')}</b><p>${esc(item.finding||'')}</p>${item.evidence_spans?.length?aiEvidenceRows(item.evidence_spans,'quote'):''}</article>`).join(''):'<p>文本引文不足；已核验的公式、图表和图像见下方视觉证据。</p>'}</section>`:'';
 const evidenceGapWarning=evidenceGap?`<div class="ai-quality-warning"><b>正文引文未能通过逐字核验。</b>下面保留模型综合供你定位阅读，但正文结论目前不能视为已核实；这类结果需要完整重跑精读。${a.partial_reason?`<br>${esc(a.partial_reason)}`:''}</div>`:'';
 const formulation=a.problem_formulation||{};const orientationRows=[['要解决的问题',a.research_question||formulation.question],['为什么值得解决',a.motivation||formulation.motivation],['输入与输出',formulation.input_output||formulation.input||method.input_output],['核心思路',method.pipeline||a.overview],['怎样证明有效',a.evaluation_summary||'见“方法与证据”和“证据核验”']].filter(([,value])=>value&&String(value).trim()).map(([label,value])=>`<div><b>${esc(label)}</b><p>${esc(typeof value==='string'?value:JSON.stringify(value))}</p></div>`).join('');
 const chunkEvidence=Number.isFinite(Number(evidenceAudit.chunk_submitted_text_citations))?` · 分块引文 ${esc(evidenceAudit.chunk_verified_text_citations??0)}/${esc(evidenceAudit.chunk_submitted_text_citations)} 条通过`:'';const evidenceSource=evidenceAudit.text_evidence_strategy==='local_exact_text'?' · 本地原文定位候选，需人工确认':evidenceAudit.text_evidence_strategy==='model_returned'?' · 模型引文':'';
 const evidenceAuditMarkup=Object.keys(evidenceAudit).length?`<div class="ai-meta">证据核验 · 正文页覆盖 ${esc(evidenceAudit.verified_text_page_count??(evidenceAudit.verified_text_pages||[]).length)}/${esc(evidenceAudit.source_text_pages??0)} 页 · ${esc(evidenceAudit.verified_text_citations??0)} 条可核验引文${chunkEvidence}${evidenceSource} · 移除 ${esc(evidenceAudit.removed_citations??0)} 条不匹配引文 · 视觉观察 ${esc(evidenceAudit.visual_observations??0)} 条 / ${esc((evidenceAudit.visual_observation_pages||[]).length)} 页</div>`:'';
  const timing=data?.duration_ms?` · ${Math.round(Number(data.duration_ms)/1000)} 秒 · ${data.model_call_count||'?'} 次模型请求${data.model_call_average_ms?` · 平均响应 ${Math.round(Number(data.model_call_average_ms)/1000)} 秒，最长 ${Math.round(Number(data.model_call_max_ms||0)/1000)} 秒`:''}`:'';
  target.innerHTML=`<div class="ai-meta">全文精读 · ${esc(data?.page_count||'')} 页 · ${esc(data?.model||'模型')} · ${esc(fmtDate(data?.created_at,true))}${timing}</div>${aiModelCallDetails(data)}${legacyUnverified?`<div class="ai-quality-warning"><b>旧版精读未通过当前校验。</b>以下内容仅供回看，页码与结构不保证完整；请对照原 PDF，或重新生成精读。${data?.analysis_warning?`<br>${esc(data.analysis_warning)}`:''}</div>`:''}${data?.low_quality_pages?`<div class="ai-quality-warning">有 ${esc(data.low_quality_pages)} 个低质量或空白页，相关结论可能不完整，请重点核对 PDF。</div>`:''}${evidenceGapWarning}${!partial&&!legacyUnverified&&!textEvidence.length?'<div class="ai-quality-warning">本次精读没有留下可核验的正文引文锚点；研究问题、方法和结论请回到 PDF 原文核对。</div>':''}${evidenceAuditMarkup}${renderPrereadingGuide(a.prereading_guide)}${partial&&!evidenceGap?partialContent:`${orientationRows?`<section class="ai-orientation-panel" data-deep-stage="overview"><div class="ai-subtitle">先看懂：读完这一栏应该能回答</div><div class="ai-orientation-grid">${orientationRows}</div></section>`:''}<div class="ai-deep-overview">${esc(a.overview||'原文未说明')}</div><div class="ai-deep-grid"><div><b>研究问题与动机</b><p>${esc(a.research_question||'原文未说明')}</p><p>${esc(a.motivation||'原文未说明')}</p></div><div><b>方法流程</b><p>${esc(method.pipeline||'原文未说明')}</p><ul>${list(method.components)}</ul></div><div><b>主要发现</b><ul>${list(a.main_findings)}</ul></div><div><b>局限与风险</b><ul>${list(a.limitations)}</ul></div><div><b>批判性阅读</b><ul>${list(critical.weak_points)}</ul></div><div><b>复现信息</b><ul>${list((a.reproducibility||{}).missing_information)}</ul></div></div>${aiDeepStructuredPanel(data,a)}`}${!partial&&textEvidence.length?`<div class="ai-subtitle">文字证据锚点</div><div class="ai-evidence-list">${aiEvidenceRows(textEvidence,'quote')}</div>`:''}${aiVisualPanel(data,a)}<div class="ai-hint">AI 推断会在正文中标注；公式、图表和图像结论请根据页面证据回到 PDF 核对。</div>`;
}

function renderReadingAnalysisPanel(paper,state={}){
 const analyses=Array.isArray(state.analyses)?state.analyses:[];
 const jobs=Array.isArray(state.jobs)?state.jobs:[];
 const artifact=analyses.find(item=>item.artifact_type==='deep_read');
 const guide=analyses.find(item=>item.artifact_type==='prereading_guide');
 const latest=jobs.find(item=>['queued','running'].includes(item.status))||jobs[0];
 const hasSource=Boolean(paper.has_fulltext_source||paper.has_local_pdf||(paper.attachments||[]).length);
 const running=Boolean(latest&&['queued','running'].includes(latest.status));
 const disabled=!agentConfigured||!hasSource||running;
 const modelAttrs=`data-model-required data-source-ready="${hasSource?'true':'false'}" data-running="${latest&&['queued','running'].includes(latest.status)?'true':'false'}" data-model-title="模型已就绪，可按阶段运行"`;
 let content='';
 if(artifact){const result=document.createElement('div');renderAiDeepRead(artifact,result);content=result.innerHTML;}
 else if(guide){content=renderPrereadingGuide(guide.analysis?.prereading_guide||guide.analysis||{});}
 else if(state.loading)content='<div class="deepread-loading"><span class="spinner"></span><b>正在读取已保存的全文分析</b></div>';
 else content='<div class="reading-analysis-empty">还没有已保存的全文精读结果。可以在这里启动分析，结果会保存在论文记录中。</div>';
 const hint=!hasSource?'添加并提取本地 PDF 后可使用全文分析':agentConfigured?'模型已就绪':'请先配置模型';
 const initialStatus=state.error?`<span class="ai-error">分析记录读取失败：${esc(state.error)} <button class="text-link" data-reading-analysis-retry="${esc(paper.id)}">重试</button></span>`:latest?`${esc(aiJobLabel(latest))} · ${esc(aiJobStatusText(latest))}${latest.message?` · ${esc(latest.message)}`:''}`:'没有运行中的全文分析任务';
 return `<section class="reader-section reading-analysis-section" id="reading-analysis-section"><div class="reading-record-heading"><div><h4>全文分析与精读</h4><span>已保存的精读结果直接显示在这里；逐段 PDF 对照可从页面上方进入</span></div></div><label class="check-label reading-fulltext-consent"><input id="deepread-consent" type="checkbox"> 同意将本篇 PDF 提取文字发送到模型；多模态精读还会发送抽样页面图像</label><div class="reading-analysis-actions"><button class="button small" ${modelAttrs} data-deepread-prereading="${esc(paper.id)}" ${disabled?'disabled':''}>生成读前准备</button><button class="button small" ${modelAttrs} data-deepread-card="${esc(paper.id)}" ${disabled?'disabled':''}>生成并填充阅读卡</button><button class="button small primary" ${modelAttrs} data-deepread-generate="${esc(paper.id)}" ${disabled?'disabled':''}>${artifact?'重新生成全文精读':'开始全文精读'}</button><span class="ai-hint">${esc(hint)}</span></div><div class="ai-job-status" id="reading-deepread-status">${initialStatus}</div><div class="reading-analysis-content" id="reading-deepread-result">${content}</div></section>`;
}

async function loadReadingPaperAnalysis(id,force=false){
 if(!id)return;
 const cached=readingAnalysisCache.get(id);
 if(cached?.loading||(!force&&cached?.loadedAt&&Date.now()-cached.loadedAt<60000))return;
 readingAnalysisCache.set(id,{...cached,loading:true,error:''});
 let allJobs=[];
 try{
  const [analysis,jobData]=await Promise.all([
   api(`/api/papers/${encodeURIComponent(id)}/ai-analysis?limit=20`),
   api(`/api/papers/${encodeURIComponent(id)}/ai-jobs?limit=30`)
  ]);
  allJobs=jobData.jobs||[];
  const analysisJobs=allJobs.filter(item=>['deep_read','reading_card','prereading_guide'].includes(item.job_type));
  readingAnalysisCache.set(id,{analyses:analysis.analyses||[],jobs:analysisJobs,loading:false,error:'',loadedAt:Date.now()});
 }catch(error){
  readingAnalysisCache.set(id,{...cached,loading:false,error:error.message||'读取失败',loadedAt:Date.now()});
 }
 if(activeView==='reading'&&selectedPaperId===id){
  renderView();
  const state=readingAnalysisCache.get(id),active=(state?.jobs||[]).find(item=>['queued','running'].includes(item.status)),briefJob=allJobs.find(item=>item.job_type==='abstract_brief'&&['queued','running'].includes(item.status));
  if(briefJob){renderAiJobStatus(briefJob);watchAiJob(id,briefJob.id);}
  if(active){renderAiJobStatus(active);watchDeepReadJob(id,active.id);}
 }
}

function aiJobLabel(job){return job?.job_type==='abstract_brief'?'摘要助读':job?.job_type==='reading_card'?'阅读卡':job?.job_type==='prereading_guide'?'读前准备':job?.job_type==='paragraph_explain'?'段落精读':job?.job_type==='reading_segmentation'?'阅读块语义整理':'全文精读';}
function aiJobStatusText(job){return ({queued:'排队中',running:'运行中',success:'已完成',partial:'部分完成',failed:'失败',cancelled:'已取消',interrupted:'上次运行中断'})[job?.status]||job?.status||'未知';}
function renderAiJobStatus(job){const target=job?.job_type==='abstract_brief'&&activeView==='reading'?$('#reading-brief-status'):$('#ai-job-status')||$('#reading-deepread-status');if(!target||!job||(job.paper_id&&activeModalPaperId&&job.paper_id!==activeModalPaperId))return;const total=Number(job.progress_total||0),current=Math.min(total,Number(job.progress_current||0)),pct=total?Math.round(current/total*100):['success','partial'].includes(job.status)?100:0;const tone=['failed','interrupted','cancelled'].includes(job.status)?'ai-error':job.status==='partial'?'ai-partial-status':job.status==='success'?'evaluation-status':'';const retryable=['failed','interrupted','cancelled','partial'].includes(job.status);const synthesisOnly=job.job_type==='deep_read'&&(Boolean(job.options?.synthesis_only_artifact_id)||(job.status==='partial'&&job.options?.synthesis_retry_available!==false));const retryLabel=synthesisOnly?'仅重新汇总':'重新运行';target.innerHTML=`<div class="ai-job-line"><span class="${tone}"><b>${esc(aiJobLabel(job))} · ${esc(aiJobStatusText(job))}</b>${job.message?` · ${esc(job.message)}`:''}</span><span>${total?`${current}/${total} · ${pct}%`:''}${job.duration_ms?` · ${Math.round(Number(job.duration_ms)/1000)} 秒`:''}</span></div>${total&&job.status==='running'?`<div class="ai-job-progress"><i style="width:${pct}%"></i></div>`:''}${job.error?`<div class="${job.status==='partial'?'ai-quality-warning':'ai-error'}">${esc(job.error)}</div>`:''}${['queued','running'].includes(job.status)?`<button class="button small" data-cancel-ai-job="${esc(job.id)}">取消任务</button>`:''}${retryable?`<button class="button small" data-retry-ai-job="${esc(job.id)}">${retryLabel}</button>`:''}`;}
async function watchAiJob(paperId, jobId){
 if(aiJobPollers[paperId]?.jobId===jobId)return;
 if(aiJobPollers[paperId]?.timer)clearTimeout(aiJobPollers[paperId].timer);
 const state={jobId,timer:null};aiJobPollers[paperId]=state;
 const tick=async()=>{try{const job=await api(`/api/ai-jobs/${jobId}`);updateGlobalAiJob(job);renderAiJobStatus(job);if(['queued','running'].includes(job.status)){state.timer=setTimeout(tick,2500);return;}delete aiJobPollers[paperId];if(['success','partial'].includes(job.status)){if(activeModalPaperId===paperId)await loadAiAnalysis(paperId);if(activeView==='prereading')await loadStudyItems(true);if(['abstract_brief','reading_card'].includes(job.job_type)&&activeModalPaperId===paperId){await refresh();await openPaper(paperId);}if(job.job_type==='abstract_brief'&&activeView==='reading'&&selectedPaperId===paperId){readingPaperDetailCache.delete(paperId);await loadReadingPaperDetail(paperId,true);}if(activeView==='reading'&&selectedPaperId===paperId&&['deep_read','prereading_guide','reading_card'].includes(job.job_type)){readingAnalysisCache.delete(paperId);if(job.job_type==='reading_card')await refresh();else await loadReadingPaperAnalysis(paperId,true);if(job.job_type==='prereading_guide')await loadStudyItems(true);}toast(job.status==='partial'?(job.fallback&&job.job_type==='abstract_brief'?'模型暂不可用，已保存本地摘要摘录；可修复配置后重试':`${aiJobLabel(job)}保留了可核验的部分结果；请查看任务提示并重试`):(job.options?.synthesis_only_artifact_id?'仅基于已保存证据完成了精读汇总':`${aiJobLabel(job)}已完成`));}else if(job.status==='failed'){toast(`${aiJobLabel(job)}失败：${job.error||'请查看任务详情'}`,'error');}}catch(err){scheduleGlobalAiJobsRefresh();state.timer=setTimeout(tick,4000);}};
 tick();
}
async function loadAiJobs(id){const target=$('#ai-job-status');if(!target)return;try{const data=await api(`/api/papers/${id}/ai-jobs?limit=10`);const jobs=data.jobs||[];const active=jobs.find(job=>['queued','running'].includes(job.status));const latest=active||jobs[0];if(latest)renderAiJobStatus(latest);else target.textContent='还没有后台 AI 任务';if(active)watchAiJob(id,active.id);}catch(e){target.textContent=`任务状态读取失败：${e.message}`;}}
async function loadAiAnalysis(id){
 const target=$('#full-ai-history');if(!target)return;
 try{
  const data=await api(`/api/papers/${id}/ai-analysis?limit=10`),rows=data.analyses||[];
  rows.forEach(item=>aiAnalyses[item.id]=item);
  const brief=rows.find(item=>item.artifact_type==='abstract_brief');
  const card=rows.find(item=>item.artifact_type==='reading_card_draft');
  const deep=rows.find(item=>item.artifact_type==='deep_read');
  const prereading=rows.find(item=>item.artifact_type==='prereading_guide');
  if(brief)renderAbstractBrief(brief);
  if(card){aiDrafts[id]={...card,draft:card.analysis?.reading_card||card.analysis};renderAiReadingCard(aiDrafts[id],$('#ai-reading-result'));}
  if(deep){aiDeepReads[id]={...deep};renderAiDeepRead(aiDeepReads[id],$('#ai-deep-result'));}
  if(prereading){
   const guide=prereading.analysis?.prereading_guide||prereading.analysis||{},result=$('#ai-prereading-result');
   if(result)result.innerHTML=`<div class="ai-meta">读前准备 · ${esc(prereading.page_count||'')} 页 · ${esc(prereading.model||'模型')} · ${esc(fmtDate(prereading.created_at,true))}${prereading.duration_ms?` · ${Math.round(Number(prereading.duration_ms)/1000)} 秒`:''}</div>${prereading.analysis_status==='legacy_unverified'?`<div class="ai-quality-warning">${esc(prereading.analysis_warning||'此历史读前准备尚未通过当前校验。')}</div>`:''}${renderPrereadingGuide(guide)}`;
  }
  const label=item=>item.artifact_type==='abstract_brief'?'摘要助读':item.artifact_type==='reading_card_draft'?(item.analysis_status==='legacy_unverified'?'阅读卡 · 旧版未校验':'阅读卡'):item.artifact_type==='prereading_guide'?(item.analysis_status==='legacy_unverified'?'读前准备 · 旧版未校验':'读前准备'):item.analysis_status==='legacy_unverified'?'精读 · 旧版未校验':item.analysis_status==='partial'?'精读 · 部分':'精读';
  target.innerHTML=rows.length?`<div>分析历史 · ${rows.length} 份</div><div class="ai-history-buttons">${rows.map(item=>`<button class="button small" data-show-ai-analysis="${esc(item.id)}">${label(item)} · ${esc(fmtDate(item.created_at,true))}</button>`).join('')}</div>`:'尚未生成 AI 分析';
 }catch(e){target.textContent=`历史分析读取失败：${e.message}`;}
}
function addFullAiAnalysisControls(id,sourceReady=false,pdfReady=false){
 const body=$('.modal-body');if(!body||body.querySelector('.full-ai-analysis'))return;
  const canRun=agentConfigured&&sourceReady,disabled=canRun?'':'disabled';
  const hint=!sourceReady?'先添加或下载本地 PDF':agentConfigured?'可按需要单独运行':modelConfigurationHint();
  const modelAttrs=`data-model-required data-source-ready="${sourceReady?'true':'false'}" data-model-title="可按需要单独运行"`;
  const readinessWarning=!sourceReady?'<div class="ai-quality-warning">全文分析需要本地 PDF 或已提取的 PDF 页文本。请先使用上方“添加本地 PDF”或“查找并下载 PDF”；仅有论文链接或摘要还不能进行全文分析。</div>':(!pdfReady?'<div class="ai-quality-warning">本地 PDF 文件目前不可用，任务会复用已保存的页文本；页面图片、公式和图表可能无法识别。重新添加 PDF 后可恢复多模态分析。</div>':'');
  const section=document.createElement('section');section.className='full-ai-analysis';section.innerHTML=`<div class="modal-section-title">全文 AI 分析</div><div class="agent-banner ai-full-banner" style="margin:0"><span class="agent-spark">✦</span><div><b>先补读前知识，再生成阅读卡或精读</b><p>读前准备只分析 PDF 提取文本，整理本文相关先修概念、专业术语、公式符号和自测题；阅读卡与精读可独立运行，缺少读前知识不会让主分析失败。深度精读会额外分析抽样页面图像。</p></div></div><label class="check-label ai-consent-label"><input id="full-ai-consent" type="checkbox"> 我同意将该论文全文文字发送到已配置的模型服务；深度精读还会发送抽样页面图像</label>${readinessWarning}<div class="ai-control-row"><button class="button small" ${modelAttrs} data-ai-prereading="${esc(id)}" ${disabled}>只生成读前准备</button><button class="button small" ${modelAttrs} data-ai-reading-card="${esc(id)}" ${disabled}>自动生成阅读卡</button><button class="button small primary" ${modelAttrs} data-ai-deep-read="${esc(id)}" ${disabled}>开始多模态 AI 精读</button><span class="ai-hint" data-model-hint data-source-ready="${sourceReady?'true':'false'}">${hint}</span></div><div id="ai-job-status" class="ai-job-status">正在读取后台任务…</div><div id="full-ai-history" class="ai-history-line">正在读取历史分析…</div><div id="ai-prereading-result" class="ai-result"></div><div id="ai-reading-result" class="ai-result"></div><div id="ai-deep-result" class="ai-result"></div>`;body.append(section);loadAiAnalysis(id);loadAiJobs(id);
}

async function loadReferences(id){
 try{const data=await api(`/api/papers/${id}/references`);const target=$('#citation-list');if(!target)return;target.innerHTML=data.count?data.references.map(r=>`<div class="source-line"><span>${esc(r.target_external_id)}</span><span>${esc(r.provider)} · ${fmtDate(r.last_seen_at,true)}</span></div>`).join(''):'尚未保存引用线索';}
 catch(e){const target=$('#citation-list');if(target)target.textContent=e.message;}
}

async function loadConnections(id){
 try{const data=await api(`/api/papers/${id}/connections`);const target=$('#connection-list');if(!target)return;const topics=(data.topics||[]).filter(item=>item.score>0).slice(0,5);const related=(data.related||[]).slice(0,5);target.innerHTML=(topics.length?`<div class="connection-group"><b>主题关联</b>${topics.map(item=>`<div class="connection-line"><span>${esc(item.name)}</span><span class="connection-tier ${item.tier==='高相关'?'high':item.tier==='邻近方向'?'near':''}">${esc(item.tier)} · ${item.score}</span></div>`).join('')}</div>`:'<div class="connection-group">尚未命中已保存主题</div>')+(related.length?`<div class="connection-group"><b>馆藏相近论文</b>${related.map(item=>`<div class="connection-line"><span class="connection-title" data-open-paper="${item.id}">${esc(item.title)}</span><span>${esc(item.tier)} · ${item.score}</span></div>`).join('')}</div>`:'');}
 catch(e){const target=$('#connection-list');if(target)target.textContent=e.message;}
}

function extractionQualityLabel(value){return value==='good'?'可检索':value==='low'?'低质量':'空页';}
function pageSnippet(text, query){const value=String(text||'');const needle=String(query||'').toLowerCase();const at=needle?value.toLowerCase().indexOf(needle):-1;if(at<0)return value.slice(0,240);return `${at>0?'…':''}${value.slice(Math.max(0,at-90),Math.min(value.length,at+String(query).length+150))}${at+String(query).length+150<value.length?'…':''}`;}
function renderPageSearchResults(target, data){const pages=data.pages||[],warning=data.retrieval_warning?`<div class="semantic-search-note">${esc(data.retrieval_warning)}</div>`:'';target.innerHTML=warning+(pages.length?pages.map(page=>`<div class="evidence-result"><div class="evidence-result-head"><b>第 ${page.page_number} 页</b><span class="evidence-quality ${page.quality}">${page.retrieval_method==='semantic'?'语义':page.retrieval_method==='hybrid'?'语义 + 词法':extractionQualityLabel(page.quality)}${page.semantic_score!=null?` · ${Number(page.semantic_score).toFixed(3)}`:''} · ${page.char_count} 字</span></div><p>${esc(page.snippet||'')}</p><small>文本哈希 ${esc(String(page.text_sha256||'').slice(0,12))}… · 证据偏移 ${page.evidence_anchor?.start_char??0}-${page.evidence_anchor?.end_char??0}</small></div>`).join(''):'<div class="evidence-empty">没有命中已提取文本；可以换一个术语，或先运行文本提取。</div>');}
async function searchDocumentPages(id, query, semantic=false){const target=$('#page-search-results');if(!target)return;target.innerHTML='<span class="spinner"></span> 正在检索页码…';try{const data=await api(`/api/papers/${id}/pages?q=${encodeURIComponent(query)}&limit=20${semantic?'&semantic=1':''}`);renderPageSearchResults(target,data);}catch(e){target.textContent=e.message;}}
function renderSemanticIndexStatus(id,data){const target=$('#semantic-index-status'),button=$(`[data-semantic-index="${CSS.escape(id)}"]`),consent=$('#semantic-index-consent');if(!target)return;const job=data.active_job,configured=data.configured&&data.embedding_model;const percent=Number(job?.progress_percent||0);if(!configured){target.innerHTML='配置 Embedding 模型和 API Key 后，可启用跨语言与同义概念检索；普通 BM25 检索不受影响。';}else if(job){target.innerHTML=`<div>${esc(job.message||'正在建立语义索引')} · ${job.progress_current||0}/${job.progress_total||0} 页</div><progress max="100" value="${percent}"></progress><button class="button small" data-semantic-index-cancel="${esc(job.id)}" ${job.cancel_requested?'disabled':''}>${job.cancel_requested?'正在取消…':'取消'}</button>`;}else if(data.page_count===0){target.textContent='先提取 PDF 文字，才能建立本地页级语义索引。';}else{const latest=data.latest_job;target.textContent=`${data.indexed_pages||0}/${data.page_count||0} 页已索引 · ${data.stale_pages||0} 页待更新 · ${esc(data.embedding_model||'')}${latest?.status==='partial'?` · 上次有 ${latest.failed_pages} 页失败，可重试`:''}`;}
 const allowed=Boolean(configured&&data.page_count&&!job&&data.stale_pages>0);if(consent){consent.disabled=!configured||!data.page_count||Boolean(job)||data.stale_pages===0;consent.dataset.indexAllowed=String(allowed);}
 if(button){button.disabled=!allowed||!consent?.checked;button.textContent=job?'索引处理中…':data.stale_pages===0&&data.page_count?'语义索引已是最新':'建立 / 更新语义索引';}
 clearTimeout(semanticIndexPollers[id]);if(job&&target.isConnected)semanticIndexPollers[id]=setTimeout(()=>loadSemanticIndexStatus(id),1200);
}
async function loadSemanticIndexStatus(id){const target=$('#semantic-index-status');if(!target)return;try{renderSemanticIndexStatus(id,await api(`/api/papers/${encodeURIComponent(id)}/semantic-index`));}catch(e){target.textContent=e.message;}}
async function loadExtraction(id){const target=$('#extraction-panel');if(!target)return;try{const [data,index]=await Promise.all([api(`/api/papers/${id}/extraction`),api(`/api/papers/${encodeURIComponent(id)}/semantic-index`)]);const pages=data.pages||[];target.innerHTML=`<div class="extraction-summary"><span>附件 ${data.attachment_count||0}</span><span>已提取 ${data.extracted_attachment_count||0}</span><span>页数 ${data.page_count||0}</span><span>字符 ${data.characters||0}</span></div><div class="extraction-quality">${pages.map(item=>`<span>${esc(String(item.attachment_id).slice(0,8))} · ${item.good_pages||0} 可检索 · ${item.low_pages||0} 低质量 · ${item.empty_pages||0} 空页</span>`).join('')||'尚未提取本地 PDF'}</div><div class="semantic-index-controls"><div id="semantic-index-status" aria-live="polite"></div><div class="semantic-index-action"><label class="check-label"><input id="semantic-index-consent" type="checkbox"> 同意将已提取文字发送到 Embedding 服务</label><button class="button small" data-semantic-index="${esc(id)}" disabled>建立 / 更新语义索引</button></div></div><div class="extraction-actions"><button class="button small" data-extract-paper="${id}" ${data.attachment_count?'':'disabled'}>提取 / 重新提取</button><form class="page-search-form" data-extraction-search="${id}"><input class="field-input" name="q" placeholder="检索原文术语或概念，如 food volume" ${data.page_count?'':'disabled'}><label class="check-label"><input type="checkbox" name="semantic"> 语义 + BM25</label><button class="button small" type="submit" ${data.page_count?'':'disabled'}>检索页码</button></form></div><div id="page-search-results" class="page-search-results"><div class="evidence-empty">可按术语精确定位，也可启用语义检索查找同义表达和跨语言概念；结果保留页码与原文证据位置。</div></div>`;renderSemanticIndexStatus(id,index);}catch(e){target.textContent=e.message;}}
async function expandPaper(id){const target=$('#expansion-result');const button=$(`[data-expand-paper="${CSS.escape(id)}"]`);if(target)target.innerHTML='<span class="spinner"></span> 正在沿引用网络扩展…';if(button){button.disabled=true;button.textContent='扩展中…';}try{const data=await api(`/api/papers/${id}/expand`,{method:'POST',body:jsonBody({limit:24})});if(target)target.innerHTML=`<div class="evidence-result"><b>已发现 ${data.count} 条关系</b><p>新增 ${data.created} 篇，已有记录 ${data.duplicates} 篇。它们已经进入文献库，可在“新发现/待读”中继续分诊。</p>${(data.candidates||[]).slice(0,8).map(item=>`<div class="connection-line"><span>${esc(item.title)}</span><span>${esc(item.relation)}</span></div>`).join('')}</div>`;await refresh();}catch(e){if(target)target.textContent=e.message;}finally{if(button){button.disabled=false;button.textContent='扩展引用、被引与相似论文';}}}

function importModal(){openModal(`<div class="modal-head"><div><h3>导入已有文献</h3><p>支持 BibTeX、RIS、CSL JSON / 本平台 JSON。导入会按 DOI、arXiv ID 和规范题名做保守去重。</p></div><button class="close-button" data-close-modal>×</button></div><div class="modal-body"><div class="form-field"><label>选择导出文件</label><input class="field-input" style="height:auto;padding:12px" id="import-file" type="file" accept=".bib,.bibtex,.ris,.json,application/json,text/plain"><span class="form-help">导入不会覆盖你已保存的笔记、状态或标签。大文件建议先拆分。</span></div><div id="import-status" style="margin-top:12px;font-size:10px;color:#748178"></div><div class="modal-section-title">完整备份恢复</div><p style="font-size:9px;line-height:1.7;color:#87938a;margin:0 0 9px">恢复会替换当前文献库。系统会先在本机备份当前数据库；请确认所选文件是本平台创建的 ZIP 备份。单个上传请求上限约 270 MB。</p><input class="field-input" style="height:auto;padding:11px" id="restore-file" type="file" accept=".zip,application/zip"><button class="button" style="margin-top:8px" data-action="restore-backup">校验并恢复备份</button></div><div class="modal-foot"><button class="button" data-close-modal>完成</button></div>`);}

const addPaperModalWithoutAssist = addPaperModal;
addPaperModal = function(){
 addPaperModalWithoutAssist();
 const form=$('#add-paper-form'), body=$('.modal-body',form);
 if(body)body.insertAdjacentHTML('afterbegin',`<section class="metadata-assist"><div class="metadata-assist-head"><b>自动补全文献信息</b><button class="button small" type="button" data-action="paper-metadata-ai">✦ 识别并填写</button></div><textarea class="field-area" id="paper-citation" name="citation" placeholder="可粘贴 DOI、引用格式或论文首页文字；也可只填标题或 DOI 后点击识别"></textarea><div id="paper-metadata-status" class="form-help">优先匹配 Crossref 公开元数据；粘贴引用文本时可调用已配置模型抽取字段。</div></section>`);
 restoreMetadataDraftJob();
};

function pdfImportModal(){
 openModal(`<div class="modal-head"><div><h3>批量导入 PDF</h3><p>自动创建或匹配文献记录、保存 PDF、提取分页文本并补全文献信息。单篇最多 100 MB，批次最多 180 MB。</p></div><button class="close-button" data-close-modal>×</button></div><div class="modal-body"><form id="pdf-upload-form"><div class="form-field"><label>选择一个或多个 PDF</label><input class="field-input" style="height:auto;padding:11px" id="pdf-upload-files" type="file" multiple accept=".pdf,application/pdf"><span class="form-help">相同 PDF 会按内容哈希去重；上传完成后会进入“新发现”，你可以继续分诊。</span></div><div class="pdf-ingest-options"><label class="check-label"><input type="checkbox" name="auto_ai" checked> 使用已配置模型补齐缺失元数据</label><input class="field-input" name="topic" placeholder="研究主题（可选）"></div><div class="modal-foot pdf-upload-foot"><button class="button primary" type="submit">上传并自动入库</button></div></form><section class="pdf-folder-setup"><div class="modal-section-title">监控本地论文文件夹</div><div class="form-field"><label for="pdf-folder-path">LitLoom 所在电脑上的文件夹路径</label><input class="field-input" id="pdf-folder-path" placeholder="例如 D:\\Research\\Papers"><span class="form-help">把 PDF 放入这个文件夹即可自动扫描。该目录在服务器所在电脑上；远程多人部署时需配置服务器可访问的共享目录。</span></div><div class="pdf-ingest-options"><label class="check-label"><input id="pdf-folder-recursive" type="checkbox" checked> 包括子文件夹</label><label class="check-label"><input id="pdf-folder-auto-ai" type="checkbox" checked> 使用模型补全元数据</label><label class="check-label"><input id="pdf-folder-enabled" type="checkbox" checked> 自动监控</label><select class="field-select" id="pdf-folder-interval"><option value="5">每 5 分钟</option><option value="15">每 15 分钟</option><option value="30" selected>每 30 分钟</option><option value="60">每小时</option></select><button class="button small" type="button" data-action="pdf-folder-scan">保存设置并立即扫描</button></div><div id="pdf-folder-list" class="pdf-folder-list"></div></section><section class="pdf-job-section"><div class="modal-section-title">导入任务与历史</div><div id="pdf-ingest-jobs" aria-live="polite"><span class="spinner"></span> 正在读取任务…</div></section></div><div class="modal-foot"><button class="button" data-close-modal>关闭</button></div>`);
 loadPdfIngestJobs(true);
 startPdfIngestPolling();
}

let modelProviderCatalog = [];
document.addEventListener('formdata',event=>{if(event.target.id!=='model-settings-form')return;const enabled=event.target.elements.namedItem('deepseek_search_enabled');if(enabled)event.formData.set('deepseek_search_enabled',enabled.checked?'1':'');});
function syncModelApiKeyRequirement(form){const provider=form?.elements.provider,key=form?.elements.api_key,clear=form?.elements.clear_api_key;if(!provider||!key)return;const changed=String(provider.value||'').trim().toLocaleLowerCase()!==String(provider.dataset.currentProvider||'').trim().toLocaleLowerCase();const required=provider.dataset.currentKey==='true'&&changed&&!clear?.checked;key.required=Boolean(required);key.placeholder=required?'切换供应商后请填写新 API Key':(provider.dataset.currentKey==='true'?'已配置，留空表示保持不变':'填写 API Key');}
function agentConfigModal(){Promise.all([api('/api/settings/model'),api('/api/settings/model/providers')]).then(([s,catalog])=>{modelProviderCatalog=catalog.providers||[];applyModelConfiguration(s);const currentPreset=modelProviderCatalog.find(item=>item.name===s.provider);const presetOptions=['<option value="">手动填写 / 自定义</option>',...modelProviderCatalog.map(item=>`<option value="${esc(item.id)}" ${currentPreset?.id===item.id?'selected':''}>${esc(item.name)}</option>`)].join('');const modelHints=modelProviderCatalog.flatMap(item=>item.models||[]).map(model=>`<option value="${esc(model)}">`).join('');openModal(`<div class="modal-head"><div><h3>模型配置中心</h3><p>支持任意 OpenAI 兼容服务。选择预设只会填入可编辑的地址和模型，不会保存或共享密钥。</p></div><button class="close-button" data-close-modal>×</button></div><form id="model-settings-form"><div class="modal-body"><div class="form-grid"><div class="form-field full"><label>快速选择接口</label><select class="field-input" id="model-provider-preset">${presetOptions}</select><span class="form-help">协议按当前服务的兼容接口选择；自定义中转地址请以服务商文档为准，切换供应商后填写对应 API Key。</span></div><div class="form-field"><label>供应商名称</label><input class="field-input" name="provider" data-current-provider="${esc(s.provider||'')}" data-current-key="${s.api_key_set?'true':'false'}" value="${esc(s.provider||'')}" placeholder="任意供应商名称"></div><div class="form-field"><label>官网链接（可选）</label><input class="field-input" name="website_url" value="${esc(s.website_url||'')}" placeholder="https://example.com"></div><div class="form-field full"><label>Base URL（可选，服务根地址）</label><input class="field-input" name="base_url" value="${esc(s.base_url||'')}" placeholder="https://api.example.com/v1"><span class="form-help">可只填写下面的 API 请求地址；两者至少填写一个。</span></div><div class="form-field full"><label>API 请求地址（可选，完整 POST 端点）</label><input class="field-input" name="api_url" value="${esc(s.api_url||'')}" placeholder="https://api.example.com/v1/chat/completions"><span class="form-help">填写后会原样调用，支持供应商自定义路径；留空时才根据 Base URL 自动拼接协议路径。</span></div><div class="form-field"><label>协议</label><select class="field-input" name="protocol"><option value="chat_completions" ${s.protocol!=='responses'?'selected':''}>Chat Completions</option><option value="responses" ${s.protocol==='responses'?'selected':''}>Responses</option></select></div><div class="form-field"><label>Model</label><input class="field-input" name="model" list="model-provider-hints" value="${esc(s.model||'')}" placeholder="模型 ID" required><datalist id="model-provider-hints">${modelHints}</datalist></div><div class="form-field"><label>Temperature</label><input class="field-input" name="temperature" type="number" min="0" max="2" step="0.1" value="${s.temperature??0.2}"></div><div class="form-field full"><label>API Key</label><input class="field-input" name="api_key" type="password" placeholder="${s.api_key_set?'已配置，留空表示保持不变':'填写 API Key'}" autocomplete="off"><span class="form-help">当前来源：${esc(s.api_key_source||'未配置')}。密钥不会回显。</span><label class="check-label" style="margin-top:8px"><input type="checkbox" name="persist_api_key" ${s.api_key_source==='环境变量'?'':'checked'}> 保存到本地密钥文件，服务更新或重启后自动读取</label><label class="check-label" style="margin-top:6px"><input type="checkbox" name="clear_api_key"> 清除本地密钥（API Key 留空时生效）</label></div><div class="form-field full"><label class="check-label"><input type="checkbox" name="enabled" ${s.enabled?'checked':''}> 启用模型助读</label></div></div><div id="model-config-status" style="margin-top:12px;font-size:10px;color:#748178"></div></div><div class="modal-foot"><button class="button" type="button" data-model-test>快速连通</button><button class="button" type="button" data-model-test="deep">首 token 实测</button><button class="button" type="button" data-close-modal>取消</button><button class="button primary" type="submit">保存配置</button></div></form>`,true);syncModelApiKeyRequirement($('#model-settings-form'));}).catch(e=>toast(e.message,'error'));}

async function createBackup(){try{toast('正在生成并校验备份…');const b=await api('/api/backup',{method:'POST',body:'{}'});const a=document.createElement('a');a.href=b.download_url;a.download=b.filename;document.body.append(a);a.click();a.remove();toast(`备份已生成：${b.filename}`);}catch(e){toast(e.message,'error');}}

const agentConfigModalWithoutEmbeddings=agentConfigModal;
agentConfigModal=function(){
 const root=$('#modal-root');let observer,timer;
 const inject=async()=>{
  const form=$('#model-settings-form');
  if(!form||form.dataset.advancedModelFields)return;
  form.closest('.modal')?.classList.add('model-config-modal');
  form.dataset.advancedModelFields='loading';
  try{
   const settings=await api('/api/settings/model'),anchor=form.elements.api_key?.closest('.form-field');
   if(!anchor)return;
   anchor.insertAdjacentHTML('beforebegin',`<details class="form-field full model-web-tools"><summary>联网工具（可选）</summary><label class="check-label"><input type="checkbox" name="deepseek_search_enabled" ${settings.deepseek_search_enabled?'checked':''}> 启用 DeepSeek Search / Fetch</label><span class="form-help">用 DeepSeek 实时网页搜索发现线索，之后仍会用 DOI、arXiv、Crossref 或 OpenAlex 核验，不会直接把网页加入文献库。</span><div class="form-grid"><div class="form-field"><label for="deepseek-search-api-url">Search API 地址</label><input class="field-input" id="deepseek-search-api-url" name="deepseek_search_api_url" value="${esc(settings.deepseek_search_api_url||'')}" placeholder="官方地址可留空，代理按文档填写"></div><div class="form-field"><label for="deepseek-fetch-api-url">Fetch API 地址</label><input class="field-input" id="deepseek-fetch-api-url" name="deepseek_fetch_api_url" value="${esc(settings.deepseek_fetch_api_url||'')}" placeholder="可选，用于抓取网页 Markdown"></div></div></details>`);
   anchor.insertAdjacentHTML('beforebegin',`<details class="form-field full model-route-settings"><summary>任务模型路由（可选）</summary><p class="form-help">留空时使用主模型。填写的模型 ID 共用上方 API 地址和密钥；快速任务侧重响应速度，论文分析与视觉模型可分别指定。</p><div class="form-grid"><div class="form-field"><label for="fast-model">快速任务模型</label><input class="field-input" id="fast-model" name="fast_model" value="${esc(settings.fast_model||'')}" placeholder="默认：${esc(settings.model||'主模型')}"><span class="form-help">检索规划、初筛、摘要和元数据</span></div><div class="form-field"><label for="analysis-model">论文分析模型</label><input class="field-input" id="analysis-model" name="analysis_model" value="${esc(settings.analysis_model||'')}" placeholder="默认：${esc(settings.model||'主模型')}"><span class="form-help">阅读卡、读前知识和全文综合</span></div><div class="form-field"><label for="vision-model">多模态视觉模型</label><input class="field-input" id="vision-model" name="vision_model" value="${esc(settings.vision_model||'')}" placeholder="默认：${esc(settings.model||'主模型')}"><span class="form-help">仅用于已授权的公式、图表和页面图像分析</span></div></div></details><div class="form-field"><label for="embedding-model">Embedding 模型 ID（可选）</label><input class="field-input" id="embedding-model" name="embedding_model" value="${esc(settings.embedding_model||'')}" placeholder="服务商提供的向量模型 ID"><span class="form-help">用于跨语言、同义表达的语义检索；与文本生成模型分开配置。</span></div><div class="form-field"><label for="embedding-api-url">Embedding API 请求地址</label><input class="field-input" id="embedding-api-url" name="embedding_api_url" value="${esc(settings.embedding_api_url||'')}" placeholder="留空时按 Base URL 自动使用 /embeddings"><span class="form-help">只在你建立索引或勾选语义检索时调用。仅发送 PDF 提取文字，不发送页面图片；按服务商规则计费。</span></div>`);
   form.dataset.advancedModelFields='ready';
  }catch{delete form.dataset.advancedModelFields;}
  finally{observer?.disconnect();clearTimeout(timer);}
 };
 if(root){observer=new MutationObserver(()=>{void inject();});observer.observe(root,{childList:true,subtree:true});timer=setTimeout(()=>observer.disconnect(),20000);}
 agentConfigModalWithoutEmbeddings();
 void inject();
};

async function saveReadingCard(id,button){const updates={};$$('[data-reading-field]',button.closest('.reading-card-section')||$('#modal-root')).forEach(field=>updates[field.dataset.readingField]=field.value);try{const card=await api(`/api/papers/${id}/reading-card`,{method:'PUT',body:jsonBody(updates)});const paper=papers.find(item=>item.id===id);if(paper)paper.reading_card=card;analytics=null;await refresh();if($('#modal-root').innerHTML)await openPaper(id);else if(activeView==='reading')renderView();toast(`阅读卡已保存 · 完成 ${card.completion_pct}%`);}catch(err){toast(err.message,'error');}}

async function updateSelectedPapers(updates){const ids=[...selectedLibraryIds];if(!ids.length)return;try{const result=await api('/api/papers/batch',{method:'POST',body:jsonBody({ids,updates})});selectedLibraryIds.clear();await refresh();analytics=null;toast(`已更新 ${result.updated} 篇${result.skipped?.length?`，跳过 ${result.skipped.length} 篇不存在的记录`:''}`);}catch(err){toast(err.message,'error');}}

async function showRunDetail(id){try{const run=await api(`/api/runs/${id}`);const status=run.status==='success'?'完成':run.status==='partial'?'部分完成':run.status==='running'?'运行中':'失败';const sourceRows=(run.source_details||[]).map(source=>{const sourceStatus=source.status==='ok'?'成功':source.status==='partial'?'部分完成':'失败';return `<section class="run-detail-source"><div class="section-heading" style="margin:0 0 8px"><b>${esc(SOURCE_NAME[source.source]||source.source)}</b><span class="log-state ${source.status==='ok'?'':'failed'}">${sourceStatus}</span></div><div class="evaluation-grid"><div><small>抓取</small><b>${source.fetched??'—'}</b></div><div><small>过滤</small><b>${source.filtered??'—'}</b></div><div><small>候选</small><b>${source.accepted??'—'}</b></div><div><small>新增 / 重复</small><b>${source.new??'—'} / ${source.duplicates??'—'}</b></div></div>${source.error?`<p class="run-detail-error">${esc(source.error)}</p>`:''}${(source.variants||[]).length?`<div class="modal-section-title">检索变体</div>${source.variants.map(v=>`<div class="source-line"><span><b>${esc(v.kind||v.id||'检索式')}</b> · ${esc(v.query||'')}</span><span>${v.fetched??0} 抓取 · ${v.accepted??0} 候选</span></div>`).join('')}`:''}</section>`;}).join('')||'<div class="empty-state" style="padding:18px">旧版运行未保存来源级详情。</div>';const timing=run.summary?.model_search_timing||run.plan?.model_search_timing||{};const timingNote=timing.duration_ms?`模型工具检索 ${Math.round(Number(timing.duration_ms)/1000)} 秒 · 模型请求 ${Math.round(Number(timing.model_request_total_ms||0)/1000)} 秒 · 工具调用 ${Math.round(Number(timing.tool_call_total_ms||0)/1000)} 秒`:'';openModal(`<div class="modal-head"><div><h3>检索运行详情</h3><p>${esc(run.query_name||'研究主题')} · ${fmtDate(run.started_at,true)} · ${status}</p></div><button class="close-button" data-close-modal>×</button></div><div class="modal-body"><div class="evaluation-grid"><div><small>新增</small><b>${run.new_count||0}</b></div><div><small>重复</small><b>${run.duplicate_count||0}</b></div><div><small>耗时</small><b>${run.started_at&&run.completed_at?`${Math.max(0,Math.round((new Date(run.completed_at)-new Date(run.started_at))/1000))} 秒`:'—'}</b></div><div><small>数据质量</small><b>${run.data_quality==='complete'?'来源详情完整':'历史详情缺失'}</b></div></div>${timingNote?`<p class="run-detail-error">${esc(timingNote)}</p>`:''}${run.query?.query?`<div class="modal-section-title">检索问题</div><p style="font-size:10px;line-height:1.7;color:#66756b">${esc(run.query.query)}</p>`:''}<div class="modal-section-title">来源与查询路径</div>${sourceRows}</div><div class="modal-foot"><button class="button" data-close-modal>关闭</button></div>`);}catch(err){toast(err.message,'error');}}

const _showRunDetailCandidateCounts=showRunDetail;
showRunDetail=async function(id){await _showRunDetailCandidateCounts(id);try{const run=await api(`/api/runs/${encodeURIComponent(id)}`);const root=$('#modal-root');if(run.candidate_mode){const summary=root?.querySelector('.modal-body > .evaluation-grid');if(summary){summary.innerHTML=`<div><small>候选论文</small><b>${Number(run.candidate_count||0)}</b></div><div><small>已导入</small><b>${Number(run.imported_count||0)}</b></div><div><small>待确认</small><b>${Number(run.awaiting_review_count||0)}</b></div><div><small>耗时</small><b>${run.started_at&&run.completed_at?`${Math.max(0,Math.round((new Date(run.completed_at)-new Date(run.started_at))/1000))} 秒`:'—'}</b></div>`;}root?.querySelectorAll('.run-detail-source .evaluation-grid').forEach(grid=>grid.lastElementChild?.remove());}if(['failed','partial','interrupted','retryable'].includes(run.status)){const foot=root?.querySelector('.modal-foot');if(foot&&!foot.querySelector('[data-discovery-history-rerun]'))foot.insertAdjacentHTML('afterbegin',`<button class="button primary" data-discovery-history-rerun="${esc(run.id||id)}">按原条件重试</button>`);}}catch{}};

async function showPaperHistory(id){try{const data=await api(`/api/papers/${id}/history`);const rows=(data.revisions||[]).map(item=>{const content=item.content||{};const preview=item.revision_type==='notes'?(content.notes||''):(content.research_question||content.contribution||'阅读卡快照');return `<div class="source-line" style="align-items:flex-start;gap:10px"><div><b>${item.revision_type==='notes'?'个人笔记':'结构化阅读卡'}</b><small style="display:block;color:#94a098;margin-top:3px">${fmtDate(item.created_at,true)} · ${esc(item.source||'user')}</small><span style="display:block;margin-top:5px;max-width:430px;white-space:pre-wrap">${esc(preview.slice(0,260))}</span></div><button class="button small" data-restore-revision="${item.id}" data-restore-paper="${id}">恢复</button></div>`}).join('')||'<div class="empty-state" style="padding:18px">还没有保存过历史版本。</div>';openModal(`<div class="modal-head"><div><h3>论文历史版本</h3><p>恢复会创建新的版本记录，现有历史不会被删除。</p></div><button class="close-button" data-close-modal>×</button></div><div class="modal-body">${rows}</div><div class="modal-foot"><button class="button" data-close-modal>关闭</button></div>`);}catch(err){toast(err.message,'error');}}

// Full-text analysis lives in a durable workspace rather than the paper
// record modal. The modal only exposes a compact status and a way back in.
const _addFullAiAnalysisControlsForWorkspace = addFullAiAnalysisControls;
addFullAiAnalysisControls = function(id, sourceReady=false, pdfReady=false){
  const body=$('.modal-body');
  if(!body||body.querySelector('.full-ai-analysis'))return;
  const artifacts=(papers.find(item=>item.id===id)?.ai_artifacts||[]);
  const labels={abstract_brief:'摘要助读',prereading_guide:'读前准备',reading_card_draft:'结构化阅读卡',deep_read:'全文精读'};
  const rows=artifacts.length?artifacts.slice(0,5).map(item=>`<div class="ai-compact-row"><span>${esc(labels[item.artifact_type]||'AI 分析')}</span><small>${esc(fmtDate(item.created_at,true))} · ${esc(item.model||'已保存')}</small></div>`).join(''):'<p class="ai-compact-empty">还没有保存的全文分析。</p>';
  const section=document.createElement('section');section.className='full-ai-analysis compact-ai-analysis';
  section.innerHTML=`<div class="modal-section-title">AI 研究资料</div><div class="ai-compact-intro"><b>分析与阅读卡分开保存</b><p>论文记录只保留元数据、笔记和入口。结构化阅读卡在阅读台维护；全文精读在精读空间按阶段展开，关闭或刷新后都能继续。</p></div><div class="ai-compact-list">${rows}</div><div class="ai-control-row"><button class="button small" data-open-reading-paper="${esc(id)}">打开阅读台</button><button class="button small primary" data-enter-deepread="${esc(id)}">进入精读空间</button>${sourceReady?`<span class="ai-hint">${pdfReady?'本地 PDF 可用于页级证据与图表核验':'已提取文字；补充 PDF 后才能核验页面图像'}</span>`:'<span class="ai-hint">先添加或下载本地 PDF 才能生成全文分析</span>'}</div>`;
  body.append(section);
};

function enterDeepRead(id){
  deepReadPaperId=id||deepReadPaperId||selectedPaperId||papers[0]?.id||'';
  selectedPaperId=deepReadPaperId;
  closeModal();
  persistUiState();
  setView('deepread');
}

function deepReadQueue(){
  const query=String(deepReadState.queueQuery||'').trim().toLowerCase();
  const activeId=deepReadPaperId||selectedPaperId;
  return papers.filter(item=>{
    // Keep the explicitly opened paper available even after it is marked read;
    // otherwise entering from a paper record silently switches to another item.
    if(!['reading','to_read','inbox','later'].includes(item.status)&&item.id!==activeId)return false;
    if(query&&!`${item.title||''} ${item.authors||''} ${item.topic||''}`.toLowerCase().includes(query))return false;
    const hasDeep=(item.ai_artifacts||[]).some(row=>row.artifact_type==='deep_read');
    const filter=deepReadState.queueFilter||'all';
    if(filter==='reading'&&item.status!=='reading'&&item.id!==activeId)return false;
    if(filter==='to_read'&&!['to_read','inbox','later'].includes(item.status)&&item.id!==activeId)return false;
    if(filter==='has_analysis'&&!hasDeep&&item.id!==activeId)return false;
    if(filter==='needs_analysis'&&hasDeep&&item.id!==activeId)return false;
    return true;
  }).sort((a,b)=>{const rank=s=>s==='reading'?0:s==='to_read'?1:s==='inbox'?2:3;return rank(a.status)-rank(b.status)||String(a.title||'').localeCompare(String(b.title||''));});
}

function paragraphAnnotationMarkup(annotation={}){
  const list=(value, renderer)=>Array.isArray(value)&&value.length?'<div class="paragraph-list">'+value.map(renderer||((item)=>'<span>'+esc(typeof item==='string'?item:JSON.stringify(item))+'</span>')).join('')+'</div>':'<span class="paragraph-muted">本段没有明确内容</span>';
  const terms=list(annotation.terms,item=>'<div class="paragraph-term"><b>'+esc(item?.term||'术语')+'</b><span>'+esc(item?.meaning||'')+'</span></div>');
  const formula=list(annotation.formula,item=>'<div class="paragraph-formula"><code>'+esc(item?.expression||item?.formula||item?.symbol||'公式/符号')+'</code><span>'+esc(item?.meaning||item?.explanation||'')+'</span></div>');
  const questions=list(annotation.questions);
  return '<div class="paragraph-ai-answer"><section><h4>一句话理解</h4><p>'+esc(annotation.explanation||'')+'</p></section><section><h4>这段在论文中的作用</h4><p>'+esc(annotation.role||'')+'</p></section><section><h4>逻辑与推理</h4><p>'+esc(annotation.logic||'')+'</p></section><section><h4>术语与符号</h4>'+terms+formula+'</section><section><h4>证据边界</h4><p>'+esc(annotation.evidence||'')+'</p></section><section><h4>继续核对</h4>'+questions+'</section></div>';
}

function renderParagraphCompare(paper){
  const blocks=deepReadState.segments||[];
  const index=Math.max(0,Math.min(Number(deepReadState.segmentIndex||0),Math.max(0,blocks.length-1)));
  const block=blocks[index];
  if(deepReadState.segmentsLoading)return '<section class="paragraph-compare"><div class="deepread-loading"><span class="spinner"></span><b>正在整理阅读块</b><small>只做本地文本分组，不调用模型。</small></div></section>';
  if(deepReadState.segmentsError)return '<section class="paragraph-compare"><div class="ai-error">'+esc(deepReadState.segmentsError)+'</div></section>';
  if(!block)return '<section class="paragraph-compare"><div class="deepread-onboarding"><h3>还没有可对照的 PDF 阅读块</h3><p>请先添加本地 PDF 并完成页级文本提取。摘要和元数据无法替代逐块原文。</p></div></section>';
  const pageStart=Number(block.page_start||block.page||0),pageEnd=Number(block.page_end||pageStart);
  const pageNumbers=[...(deepReadState.pageNumbers||[])].map(Number).filter(Boolean).filter((page,i,list)=>list.indexOf(page)===i).sort((a,b)=>a-b);
  const availablePages=pageNumbers.length?pageNumbers:[...new Set(blocks.flatMap(item=>[Number(item.page_start||item.page),Number(item.page_end||item.page_start||item.page)]).filter(Boolean))].sort((a,b)=>a-b);
  const safePage=availablePages.includes(Number(deepReadState.pdfPage||0))?Number(deepReadState.pdfPage):pageStart;
  const pageIndex=Math.max(0,availablePages.indexOf(safePage));
  const pageOptions=availablePages.map(page=>'<option value="'+page+'" '+(page===safePage?'selected':'')+'>第 '+page+' 页</option>').join('');
  const blockOptions=blocks.map((item,i)=>{const start=Number(item.page_start||item.page||0),end=Number(item.page_end||start),range=start===end?'第 '+start+' 页':'第 '+start+'–'+end+' 页',section=String(item.section_title||'章节未识别');return '<button class="paragraph-index '+(i===index?'active ':'')+(item.annotation?.explanation?'annotated':'')+'" data-paragraph-index="'+i+'" title="'+esc(section)+' · 阅读块 '+(i+1)+'：'+esc(String(item.text||'').slice(0,180))+'"><span>'+(String(i+1).padStart(2,'0'))+'</span><small><b>'+esc(section)+' · 块 '+(i+1)+'</b> · '+range+'<br>'+esc(String(item.text||'').slice(0,68))+'</small></button>';}).join('');
   const annotation=block.annotation||{};
   const running=deepReadState.paragraphJobId;
   const segmentation=deepReadState.segmentation||{mode:'structure'};
   const segmentationRunning=(deepReadState.jobs||[]).some(job=>job.job_type==='reading_segmentation'&&['queued','running'].includes(job.status));
   const segmentationForce=segmentation.mode==='hybrid_semantic';
   const segmentationLabel=segmentationRunning||deepReadState.segmentationSubmitting?'正在判断边界…':segmentationForce?'重新判断边界':segmentation.mode==='local_cohesion'?'本地判断完成':'AI 语义整理';
   const segmentationStatus=segmentation.mode==='hybrid_semantic'?`AI 已复核 ${Number(segmentation.reviewed_boundaries||0)} 个边界，合并 ${Number(segmentation.merged_boundaries||0)} 处`:segmentation.mode==='local_cohesion'?'没有发现需要模型复核的模糊边界':'当前按章节结构分块';
   const segmentationButton='<button class="button small primary" data-reading-organize="'+(segmentationForce?'true':'false')+'" '+(segmentationRunning||deepReadState.segmentationSubmitting||segmentation.mode==='local_cohesion'?'disabled':'')+'>'+segmentationLabel+'</button>';
   const annotationRefresh=block.annotation_stale?'<div class="paragraph-context-stale"><span>此前的精读记忆已更新，这一块可以同步刷新。</span><button class="button small" data-paragraph-analyze="'+esc(block.segment_key)+'" data-paragraph-force="true" '+(running?'disabled':'')+'>结合最新前文重新精读</button></div>':'';
  const prev=blocks[index-1],next=blocks[index+1];
  const attachment=(paper?.attachments||[]).find(item=>item.id===block.attachment_id)||(paper?.attachments||[])[0];
  const pdfUrl=block.pdf_url||(attachment?.stored_name?'/files/'+attachment.stored_name:'');
  const pageImageUrl=pdfUrl?'/api/papers/'+encodeURIComponent(deepReadPaperId)+'/pdf-page?attachment_id='+encodeURIComponent(attachment?.id||block.attachment_id||'')+'&page='+safePage:'';
  const pdfLink=pdfUrl?'<a class="button small ghost" href="'+esc(pdfUrl)+'#page='+safePage+'" target="_blank" rel="noopener">打开原始 PDF ↗</a>':'';
  const pdfFrame=pageImageUrl?'<figure class="paragraph-pdf-wrap"><div class="paragraph-page-stage"><img class="paragraph-pdf-page" src="'+esc(pageImageUrl)+'" alt="论文 PDF 第 '+safePage+' 页" loading="eager"></div><figcaption class="paragraph-pdf-caption"><b>PDF 第 '+safePage+' 页</b><span>当前阅读块覆盖 '+(pageStart===pageEnd?'第 '+pageStart+' 页':'第 '+pageStart+'–'+pageEnd+' 页')+'</span></figcaption></figure>':'<div class="paragraph-pdf-missing">当前论文没有可用的本地 PDF 文件。请回到论文记录添加 PDF，提取文本不能替代页面排版。</div>';
  const rangeLabel=pageStart===pageEnd?'第 '+pageStart+' 页':'第 '+pageStart+'–'+pageEnd+' 页';
  const blockLabel='阅读块 '+(index+1)+' / '+blocks.length;
   return '<section class="paragraph-compare"><div class="paragraph-compare-head"><div><span class="eyebrow">PDF 与解释绑定</span><h3>按阅读块对照精读</h3><p>当前只显示一页 PDF。左侧是原始页面和阅读块原文，右侧解释当前块，并结合论文摘要、相邻块和前文精读记忆。</p><small class="ai-hint">分块判断：'+esc(segmentationStatus)+'</small></div><div class="paragraph-location"><strong>'+blockLabel+'</strong><span>'+rangeLabel+'</span></div></div><div class="paragraph-toolbar"><div class="paragraph-page-controls"><span class="paragraph-control-label">PDF 页</span><button class="button small" data-pdf-page-prev '+(pageIndex>0?'':'disabled')+'>上一页</button><select class="field-select" data-pdf-page aria-label="选择 PDF 页码">'+pageOptions+'</select><button class="button small" data-pdf-page-next '+(pageIndex<availablePages.length-1?'':'disabled')+'>下一页</button></div><div class="paragraph-block-controls"><button class="button small" data-paragraph-prev '+(prev?'':'disabled')+'>上一块</button><button class="button small" data-paragraph-next '+(next?'':'disabled')+'>下一块</button></div>'+pdfLink+segmentationButton+'<label class="check-label"><input id="paragraph-consent" type="checkbox"> 允许发送论文标题和短文本片段</label><span class="ai-hint">'+safePage+' / '+(availablePages.length||'?')+' 页 · '+(annotation.explanation?'已保存解释':'尚未解释')+'</span></div><div class="paragraph-compare-grid"><aside class="paragraph-source-pane"><div class="paragraph-pane-title"><div><b>论文 PDF 原文</b><small>当前显示第 '+safePage+' 页</small></div><span class="paragraph-current-block">'+blockLabel+'</span></div>'+pdfFrame+'<section class="paragraph-block-source"><div class="paragraph-block-source-head"><b>当前阅读块原文</b><span>'+Number(block.paragraph_count||1)+' 个段落</span></div><blockquote class="paragraph-source-text">'+esc(block.text)+'</blockquote></section><div class="paragraph-index-title">阅读块导航 <small>点击后同步 PDF 页</small></div><div class="paragraph-index-list">'+blockOptions+'</div></aside><article class="paragraph-insight-pane"><div class="paragraph-pane-title"><div><b>AI 精读 · '+blockLabel+'</b><small>'+(annotation.explanation?(block.annotation_stale?'前文记忆已变化，可更新当前解释':'当前块已完成'):'解释当前块，并参考本论文上下文')+'</small></div><span class="paragraph-page-badge">PDF '+safePage+'</span></div>'+(annotation.explanation?paragraphAnnotationMarkup(annotation)+annotationRefresh:'<div class="paragraph-empty"><div class="paragraph-empty-icon">✦</div><h4>先回答这一块在做什么</h4><p>AI 会围绕当前论证说明任务、前提、推理、术语和公式，并明确哪些内容必须回到整篇论文核对。</p><button class="button small primary" data-paragraph-analyze="'+esc(block.segment_key)+'" '+(running?'disabled':'')+'>'+(running?'正在解释…':'结合上下文精读当前块')+'</button></div>')+'</article></div></section>';
}

async function loadReadingSegments(id=deepReadPaperId){
  if(!id)return;
  deepReadState={...deepReadState,segmentsLoading:true,segmentsError:''};
  if(activeView==='deepread')renderView();
  try{
    const data=await api('/api/papers/'+encodeURIComponent(id)+'/reading-segments?limit=3000');
    const segments=Array.isArray(data.blocks)&&data.blocks.length?data.blocks:(Array.isArray(data.segments)?data.segments:[]);
    const pageNumbers=Array.isArray(data.page_numbers)?data.page_numbers.map(Number).filter(Number.isFinite).filter((page,index,pages)=>page>0&&pages.indexOf(page)===index).sort((a,b)=>a-b):[];
    const boundedIndex=Math.max(0,Math.min(Number(deepReadState.segmentIndex||0),Math.max(0,segments.length-1)));
    const selectedPage=Number(deepReadState.pdfPage||0);
    const firstBlockPage=Number(segments[boundedIndex]?.page_start||segments[boundedIndex]?.page||0);
    const pdfPage=pageNumbers.includes(selectedPage)?selectedPage:(pageNumbers.includes(firstBlockPage)?firstBlockPage:(pageNumbers[0]||0));
    deepReadState={...deepReadState,segments,segmentation:data.segmentation||{mode:'structure'},pageNumbers,pdfPage,segmentsLoading:false,segmentIndex:boundedIndex};
    persistUiState();
    if(activeView==='deepread')renderView();
  }catch(error){deepReadState={...deepReadState,segmentsLoading:false,segmentsError:'段落读取失败：'+error.message};if(activeView==='deepread')renderView();}
}

async function requestParagraphAnalysis(segmentKey,force=false){
  const segments=deepReadState.segments||[], index=segments.findIndex(item=>item.segment_key===segmentKey), segment=segments[index];
  if(!segment)return;
  if(!$('#paragraph-consent')?.checked){toast('请先确认允许发送本论文上下文','error');return;}
  deepReadState.paragraphJobId=segmentKey;deepReadState.segmentIndex=index;persistUiState();renderView();
  try{
    const data=await api('/api/papers/'+encodeURIComponent(deepReadPaperId)+'/paragraph-analysis',{method:'POST',body:jsonBody({background:true,consent:true,segment_key:segment.segment_key,text:segment.text,language:'zh-CN',template:'paragraph_explain_v1',reuse:!force})});
    if(data.job){deepReadState.jobs=[data.job,...(deepReadState.jobs||[])];watchDeepReadJob(deepReadPaperId,data.job.id);toast('当前段落已加入后台精读');}
    else{await loadReadingSegments(deepReadPaperId);toast('当前段落解释已保存');}
  }catch(error){toast('段落精读失败：'+error.message,'error');}
  finally{if(!deepReadState.jobs.some(job=>job.id&&job.job_type==='paragraph_explain'&&['queued','running'].includes(job.status)&&job.options?.segment_key===segmentKey)){deepReadState.paragraphJobId='';}if(activeView==='deepread')renderView();}
}

async function requestReadingSegmentation(force=false){
  if(!$('#paragraph-consent')?.checked){toast('请先确认允许发送论文段落摘要','error');return;}
  if(deepReadState.segmentationSubmitting)return;
  deepReadState.segmentationSubmitting=true;renderView();
  try{
    const data=await api('/api/papers/'+encodeURIComponent(deepReadPaperId)+'/ai-reading-segmentation',{method:'POST',body:jsonBody({background:true,consent:true,force})});
    if(data.job){deepReadState.jobs=[data.job,...(deepReadState.jobs||[])];watchDeepReadJob(deepReadPaperId,data.job.id);toast(data.deduplicated?'阅读块整理任务正在运行':'已开始判断阅读块边界');}
    else{await loadReadingSegments(deepReadPaperId);toast('阅读块语义整理已完成');}
  }catch(error){toast('阅读块整理失败：'+error.message,'error');}
  finally{deepReadState.segmentationSubmitting=false;if(activeView==='deepread')renderView();}
}

function renderDeepRead(){
  const filterAliases={results:'critique',visuals:'evidence'};
  if(filterAliases[deepReadState.filter]){deepReadState.filter=filterAliases[deepReadState.filter];persistUiState();}
  const queue=deepReadQueue();
  const selected=queue.find(item=>item.id===deepReadPaperId)||queue.find(item=>item.id===selectedPaperId)||queue[0];
  if(selected&&selected.id!==deepReadPaperId){deepReadPaperId=selected.id;selectedPaperId=selected.id;persistUiState();}
  const queueRows=queue.length?queue.map(item=>{const hasDeep=(item.ai_artifacts||[]).some(row=>row.artifact_type==='deep_read');return `<button class="deepread-queue-row ${item.id===deepReadPaperId?'active':''}" data-deepread-paper="${esc(item.id)}"><span class="deepread-queue-status ${item.status}"></span><span><b>${esc(item.title)}</b><small>${esc([item.year,STATUS[item.status]].filter(Boolean).join(' · '))}${hasDeep?' · 已有精读':''}</small></span></button>`;}).join(''):'<div class="empty-state" style="padding:22px 12px">没有符合条件的待读论文。<br><button class="button small" data-nav="library">去文献库选择</button></div>';
  const paper=deepReadState.paper&&deepReadState.paper.id===deepReadPaperId?deepReadState.paper:selected;
  const job=deepReadState.jobs.find(item=>['queued','running'].includes(item.status))||deepReadState.jobs[0];
  const hasSource=Boolean(paper?.has_fulltext_source||paper?.has_local_pdf||(paper?.attachments||[]).length);
  const running=job&&['queued','running'].includes(job.status);
  const deepreadHint=!hasSource?'请先添加或下载本地 PDF':running?'全文精读任务正在运行':agentConfigured?'模型已就绪，可按阶段运行':modelConfigurationHint();
  const deepreadModelAttrs=`data-model-required data-source-ready="${hasSource?'true':'false'}" data-running="${running?'true':'false'}" data-model-title="模型已就绪，可按阶段运行"`;
  const controls=paper?`<label class="check-label deepread-consent"><input id="deepread-consent" type="checkbox"> 同意将本篇全文发送到已配置模型</label><div class="deepread-actions"><button class="button small" ${deepreadModelAttrs} data-deepread-prereading="${esc(paper.id)}" ${!agentConfigured||!hasSource?'disabled':''}>先生成读前准备</button><button class="button small" ${deepreadModelAttrs} data-deepread-card="${esc(paper.id)}" ${!agentConfigured||!hasSource?'disabled':''}>生成结构化阅读卡</button><button class="button small primary" ${deepreadModelAttrs} data-deepread-generate="${esc(paper.id)}" ${!agentConfigured||!hasSource||running?'disabled':''}>${running?'精读任务运行中…':'开始全文精读'}</button><span class="ai-hint" data-model-hint>${esc(deepreadHint)}</span></div>`:'';
  const stages=[['overview','先看懂'],['prereading','读前准备'],['paragraph','逐段对照'],['method','方法与证据'],['evidence','证据核验'],['critique','批判与复现']];
  const stageNav=stages.map(([key,label])=>`<button class="deepread-stage ${deepReadState.filter===key?'active':''}" data-deepread-filter="${key}">${label}</button>`).join('');
  return `${viewHead('DEEP READING WORKSPACE','精读空间','一篇论文只走一条清晰路径：先看懂，再补知识，逐段对照，核验方法证据，最后批判与复现。',`<button class="button" data-nav="reading">退出精读</button>`)}<div class="deepread-layout"><aside class="surface-card deepread-queue"><div class="deepread-queue-head"><div><h3>精读队列</h3><small>${queue.length} 篇 · 只显示待读与正在读</small></div><select class="field-select" id="deepread-queue-filter"><option value="all" ${deepReadState.queueFilter==='all'?'selected':''}>全部</option><option value="reading" ${deepReadState.queueFilter==='reading'?'selected':''}>正在读</option><option value="to_read" ${deepReadState.queueFilter==='to_read'?'selected':''}>待读</option><option value="needs_analysis" ${deepReadState.queueFilter==='needs_analysis'?'selected':''}>待精读</option><option value="has_analysis" ${deepReadState.queueFilter==='has_analysis'?'selected':''}>已有精读</option></select></div><input class="field-input" id="deepread-queue-search" value="${esc(deepReadState.queueQuery||'')}" placeholder="筛选标题、作者或主题…">${queueRows}</aside><main class="surface-card deepread-main">${paper?`<div class="deepread-paper-head"><div><span class="eyebrow">${esc(STATUS[paper.status]||'待读')} · ${hasSource?'全文已就绪':'需要 PDF'}</span><h2>${esc(paper.title)}</h2><p>${esc([paper.authors,paper.year,paper.venue].filter(Boolean).join(' · ')||'作者与出版信息待补充')}</p></div><div class="deepread-head-actions"><button class="button small" data-open-paper="${esc(paper.id)}">论文记录</button>${paper.url?`<a class="button small" href="${esc(paper.url)}" target="_blank" rel="noopener">来源 ↗</a>`:''}</div></div><div class="deepread-orientation"><div><b>从 0 到能讲清</b><span>每一栏只回答一个问题；AI 解释都保留原文位置和证据范围，读不懂时直接进入“逐段对照”。</span></div><div class="deepread-step-list"><span>1 先看懂</span><span>2 读前准备</span><span>3 逐段对照</span><span>4 方法与证据</span><span>5 批判与复现</span></div></div>${controls}<div id="deepread-job-status" class="ai-job-status">${job?`${esc(aiJobLabel(job))} · ${esc(aiJobStatusText(job))}${job.message?` · ${esc(job.message)}`:''}`:'还没有全文精读任务'}</div><nav class="deepread-stage-nav" aria-label="精读章节">${stageNav}</nav>${deepReadState.filter==='paragraph'?renderParagraphCompare(paper):''}${deepReadState.filter==='paragraph'?'': '<div id="deepread-result" class="deepread-result">正在读取已保存精读…</div>'}`:`<div class="deepread-empty">${emptyState('先选择一篇论文','精读空间会保存逐页证据、方法拆解和批判性阅读结果。','<button class="button small primary" data-nav="library">打开文献库</button>')}</div>`}</main></div>`;
}

function applyDeepReadFilter(){
  const target=$('#deepread-result');if(!target)return;
  const filter=deepReadState.filter||'overview';
  const stageForCard=(label)=>{
    const value=String(label||'');
    if(value.includes('方法流程')||value.includes('方法')||value.includes('数据')||value.includes('复现'))return 'method';
    if(value.includes('主要发现')||value.includes('局限')||value.includes('批判')||value.includes('结果')||value.includes('复现'))return 'critique';
    return 'overview';
  };
  target.querySelectorAll('.ai-deep-overview').forEach(item=>item.hidden=filter!=='overview');
  target.querySelectorAll('.ai-orientation-panel').forEach(item=>item.hidden=filter!=='overview');
  target.querySelectorAll('.ai-deep-grid > div').forEach(item=>{item.dataset.deepStage=stageForCard(item.querySelector('b')?.textContent);item.hidden=filter!=='overview'&&item.dataset.deepStage!==filter;});
  target.querySelectorAll('.ai-deep-grid').forEach(item=>item.hidden=!item.querySelector(':scope > div:not([hidden])'));
  target.querySelectorAll('.ai-deep-structured[data-deep-stage]').forEach(item=>{const stage=item.dataset.deepStage==='results'?'critique':item.dataset.deepStage;item.hidden=filter!==stage;});
  target.querySelectorAll('.ai-deep-structured-panel').forEach(item=>item.hidden=!item.querySelector('[data-deep-stage]:not([hidden])'));
  target.querySelectorAll('.ai-prereading-guide').forEach(item=>item.hidden=filter!=='prereading');
  target.querySelectorAll('.ai-visual-panel').forEach(item=>item.hidden=filter!=='evidence');
  target.querySelectorAll('.ai-evidence-list').forEach(item=>item.hidden=filter!=='evidence');
  target.querySelectorAll('.ai-subtitle').forEach(item=>{if(item.textContent.includes('文字证据'))item.hidden=filter!=='evidence';if(item.textContent.includes('视觉证据'))item.hidden=filter!=='evidence';});
}

function renderDeepReadArtifact(){
  const target=$('#deepread-result');if(!target)return;
  if(deepReadState.loading){target.innerHTML='<div class="deepread-loading"><span class="spinner"></span><b>正在恢复精读资料</b><small>只读取已保存结果，不会重新调用模型。</small></div>';return;}
  if(deepReadState.error){target.innerHTML=`<div class="ai-error">${esc(deepReadState.error)}</div>`;return;}
  const artifact=(deepReadState.analyses||[]).find(item=>item.artifact_type==='deep_read');
  const guideArtifact=(deepReadState.analyses||[]).find(item=>item.artifact_type==='prereading_guide');
  if(!artifact){
    const guide=guideArtifact?.analysis?.prereading_guide||guideArtifact?.analysis||{};
    if(guideArtifact&&deepReadState.filter!=='prereading'){deepReadState.filter='prereading';persistUiState();$$('[data-deepread-filter]').forEach(item=>item.classList.toggle('active',item.dataset.deepreadFilter==='prereading'));}
    target.innerHTML=guideArtifact?`<div class="deepread-onboarding"><div class="deepread-onboarding-icon">1</div><h3>读前准备已完成</h3><p>先掌握下面列出的先修概念、术语和符号，再开始全文精读。读前知识会独立保存到“读前知识”模块，全文精读完成后还会在论文地图中保留上下文。</p></div>${renderPrereadingGuide(guide)}`:`<div class="deepread-onboarding"><div class="deepread-onboarding-icon">⌘</div><h3>还没有全文精读</h3><p>建议按这个顺序：先生成读前准备，确认术语和符号；再开始全文精读，系统会按文本证据、公式图表和批判性核验分层保存。</p><ol><li>确认本地 PDF 已提取</li><li>勾选全文发送授权</li><li>开始后台精读，离开页面也会继续</li></ol></div>`;if(guideArtifact)applyDeepReadFilter();return;}
  const shell=document.createElement('div');renderAiDeepRead(artifact,shell);target.innerHTML='';target.append(...shell.childNodes);applyDeepReadFilter();
}

async function loadDeepReadWorkspace(id=deepReadPaperId, force=false){
  if(activeView!=='deepread')return;
  const targetId=id||deepReadPaperId||selectedPaperId||papers[0]?.id;
  if(!targetId){renderView();return;}
  if(!force&&deepReadState.paper?.id===targetId&&deepReadState.analyses.length){renderDeepReadArtifact();return;}
  deepReadPaperId=targetId;selectedPaperId=targetId;deepReadState={...deepReadState,loading:true,error:'',paper:null,analyses:[],jobs:[],segments:[],segmentsLoading:true,segmentsError:''};persistUiState();renderView();
  try{
    const [paper,analysis,jobs,segments]=await Promise.all([api(`/api/papers/${encodeURIComponent(targetId)}?view=summary`),api(`/api/papers/${encodeURIComponent(targetId)}/ai-analysis?limit=20`),api(`/api/papers/${encodeURIComponent(targetId)}/ai-jobs?limit=30`),api(`/api/papers/${encodeURIComponent(targetId)}/reading-segments?limit=3000`)]);
    const segmentRows=Array.isArray(segments.blocks)&&segments.blocks.length?segments.blocks:(Array.isArray(segments.segments)?segments.segments:[]);
    const pageNumbers=Array.isArray(segments.page_numbers)?segments.page_numbers.map(Number).filter(Number.isFinite).filter((page,index,pages)=>page>0&&pages.indexOf(page)===index).sort((a,b)=>a-b):[];
    const boundedIndex=Math.max(0,Math.min(Number(deepReadState.segmentIndex||0),Math.max(0,segmentRows.length-1)));
    const selectedPage=Number(deepReadState.pdfPage||0);
    const firstBlockPage=Number(segmentRows[boundedIndex]?.page_start||segmentRows[boundedIndex]?.page||0);
    const pdfPage=pageNumbers.includes(selectedPage)?selectedPage:(pageNumbers.includes(firstBlockPage)?firstBlockPage:(pageNumbers[0]||0));
    deepReadState={...deepReadState,loading:false,paper,analyses:analysis.analyses||[],jobs:jobs.jobs||[],segments:segmentRows,segmentation:segments.segmentation||{mode:'structure'},pageNumbers,pdfPage,segmentsLoading:false,segmentIndex:boundedIndex};persistUiState();renderView();renderDeepReadArtifact();
  }catch(error){deepReadState={...deepReadState,loading:false,error:`精读资料读取失败：${error.message}`};renderView();}
}

async function requestDeepReadStage(id, type){
  const consent=$('#deepread-consent')?.checked===true;
  if(!consent){toast('请先确认本篇全文可以发送到模型服务','error');return;}
  const endpoint=type==='deep_read'?'ai-deep-read':type==='reading_card'?'ai-reading-card':'ai-prereading-guide';
  const button=$(`[data-deepread-${type==='deep_read'?'generate':type==='reading_card'?'card':'prereading'}="${CSS.escape(id)}"]`);
  if(button){button.disabled=true;button.textContent='已加入后台任务…';}
  try{
    const data=await api(`/api/papers/${encodeURIComponent(id)}/${endpoint}`,{method:'POST',body:jsonBody({background:true,consent:true,apply:type==='reading_card',merge_empty:true,template:type==='deep_read'?'graduate_deep_read':type==='reading_card'?'fulltext_reading_card':'paper_prereading_guide',language:'zh-CN'})});
     if(data.job){deepReadState.jobs=[data.job,...(deepReadState.jobs||[])];if(activeView==='reading'){const cached=readingAnalysisCache.get(id)||{};readingAnalysisCache.set(id,{...cached,jobs:[data.job,...(cached.jobs||[])],loading:false,loadedAt:Date.now()});}renderView();renderAiJobStatus(data.job);watchDeepReadJob(id,data.job.id);toast(data.deduplicated?'已有相同任务在运行':'任务已加入后台队列');}
     else {toast('分析已完成并保存');if(activeView==='reading'){readingAnalysisCache.delete(id);await loadReadingPaperAnalysis(id,true);}else await loadDeepReadWorkspace(id,true);}
  }catch(error){toast(`${aiJobLabel({job_type:type})}失败：${error.message}`,'error');}
  finally{if(button?.isConnected){button.disabled=false;button.textContent=type==='deep_read'?'开始全文精读':type==='reading_card'?'生成结构化阅读卡':'先生成读前准备';}}
}

function watchDeepReadJob(paperId, jobId){
  const tick=async()=>{try{const job=await api(`/api/ai-jobs/${encodeURIComponent(jobId)}`);deepReadState.jobs=[job,...(deepReadState.jobs||[]).filter(item=>item.id!==job.id)];if(activeView==='reading'&&selectedPaperId===paperId)renderAiJobStatus(job);if(['queued','running'].includes(job.status)){if(activeView==='deepread')renderView();setTimeout(tick,2200);return;}if(['success','partial'].includes(job.status)){if(job.job_type==='paragraph_explain'){deepReadState.paragraphJobId='';await loadReadingSegments(paperId);toast('当前段落解释已保存');}else if(job.job_type==='reading_segmentation'){await loadReadingSegments(paperId);toast('阅读块语义整理已保存');}else if(activeView==='reading'&&selectedPaperId===paperId){readingAnalysisCache.delete(paperId);if(job.job_type==='reading_card')await refresh();else await loadReadingPaperAnalysis(paperId,true);if(job.job_type==='prereading_guide')await loadStudyItems(true);toast(job.status==='partial'?'精读保留了可核验的部分结果':'分析结果已保存到阅读台');}else{await loadDeepReadWorkspace(paperId,true);toast(job.status==='partial'?'精读保留了部分可核验结果':'精读已完成');}}else if(job.status==='failed'){deepReadState.paragraphJobId='';if(activeView==='deepread')renderView();toast(aiJobLabel(job)+'失败：'+(job.error||'请查看任务中心'),'error');}}catch{if(activeView==='deepread'||(activeView==='reading'&&selectedPaperId===paperId))setTimeout(tick,4000);}};tick();
}

