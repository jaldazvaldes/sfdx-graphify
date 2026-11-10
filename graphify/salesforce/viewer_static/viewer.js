'use strict';
const $ = id => document.getElementById(id);
const colors = {reads:'#2563b7',writes:'#a85a16',filters:'#7b48b6',calls:'#087d78',references:'#68768a',contains:'#84939d'};
const kindNames = {object:'Object',field:'Field',flow:'Flow',apexclass:'Apex class',apexmethod:'Apex method',apextrigger:'Trigger',lwc:'Lightning component',outboundmessage:'Outbound message',validationrule:'Validation rule'};
const state = {meta:null,focus:null,mode:'impact',depth:1,status:'all',history:[],view:null,edge:null,evidenceSnapshot:'after',evidenceOffset:0,request:0,searchRequest:0,diagnosticsRequest:0,evidenceRequest:0};
const positions = new Map();
let searchTimer;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function svgElement(tag, attributes, text) {
  const node = document.createElementNS('http://www.w3.org/2000/svg',tag);
  for (const [key,value] of Object.entries(attributes || {})) node.setAttribute(key,String(value));
  if (text !== undefined) node.textContent = text;
  return node;
}
async function api(path, params={}) {
  const response = await fetch(path + (Object.keys(params).length ? '?' + new URLSearchParams(params) : ''), {credentials:'same-origin',cache:'no-store'});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'The local request failed.');
  return data;
}
function showError(message) { $('error').textContent=message; $('error').hidden=!message; }
function shortId(value) { return value ? value.split(':').pop().slice(0,12) : 'No Git revision'; }
function count(value) { return Number(value || 0).toLocaleString(); }
function candidateSummary(edge) {
  const candidates=edge.resolution_counts?.candidate || 0;
  return candidates ? `${count(candidates)} candidate, ${count(edge.resolution_counts.extracted)} extracted relationships` : '';
}
function candidateDescription(resolution) {
  if(resolution?.status!=='candidate') return '';
  const missing=resolution.missing_ancestors || [];
  const reason=({external_hierarchy:'external hierarchy not verified',
    conditional_virtual_dispatch:'local virtual dispatch is conditional',
    fflib_event_dispatch:'fflib event dispatch is conditional'})[resolution.reason] || 'dispatch remains uncertain';
  return `Candidate call: ${reason}` + (missing.length ? ` · Missing ancestors: ${missing.join(', ')}` : '');
}

async function search(query) {
  const request = ++state.searchRequest;
  try {
    const data = await api('/api/search',{query});
    if(request !== state.searchRequest) return;
    $('search-results').replaceChildren();
    $('search-summary').textContent = query.trim() ? `${count(data.total)} matches${data.truncated ? ' · first 25 shown' : ''}` : '';
    for (const item of data.results) {
      const button = element('button',undefined,'search-result');
      button.append(element('strong',item.label),element('span',`${kindNames[item.kind] || item.kind}${item.local_status ? ' · '+item.local_status : ''}${item.change==='removed'?' · before snapshot only':''}`));
      button.addEventListener('click',()=>focus(item.id));
      $('search-results').append(button);
    }
  } catch(error) { if(request===state.searchRequest) showError(error.message); }
}
async function focus(id, remember=true) {
  if(state.focus && state.focus!==id && remember) state.history.push(state.focus);
  state.focus=id;
  $('back').disabled=state.history.length===0;
  $('search-results').replaceChildren(); $('search-summary').textContent='';
  await renderView();
}
function describeFocus(item) {
  $('focus-name').textContent=item.label;
  $('focus-kind').textContent=`${kindNames[item.kind] || item.kind} · ${item.definition_status.replaceAll('_',' ')}`;
  $('focus-metadata').replaceChildren();
  if(item.local_status) $('focus-metadata').append(element('p','Local XML status: '+item.local_status,'focus-attribute'));
  if(item.trigger_object) $('focus-metadata').append(element('p','Trigger object: '+item.trigger_object,'focus-attribute'));
  if(item.trigger_type) $('focus-metadata').append(element('p','Trigger type: '+item.trigger_type,'focus-attribute'));
  if(item.api_version) $('focus-metadata').append(element('p','Metadata API: '+item.api_version,'focus-attribute'));
  if(item.before_local_status && item.before_local_status!==item.local_status) $('focus-metadata').append(element('p','Before XML status: '+item.before_local_status,'focus-attribute'));
}
async function renderView() {
  if(!state.focus) return;
  const request=++state.request;
  state.edge=null; resetEvidence(); renderDiagnostics(null);
  showError(''); $('graph-wrap').classList.add('loading');
  try {
    const data=await api('/api/view',{component:state.focus,mode:state.mode,depth:state.depth,status:state.status});
    if(request!==state.request) return;
    state.view=data; state.edge=null;
    describeFocus(data.focus);
    $('view-title').textContent=data.focus.label;
    $('mode-heading').textContent=state.mode[0].toUpperCase()+state.mode.slice(1)+' view';
    $('view-description').textContent=data.interpretation;
    $('empty').hidden=true; $('graph').removeAttribute('hidden'); $('center').disabled=false;
    const notes=$('view-notes'); notes.replaceChildren();
    notes.append(element('p',`${count(data.nodes.length)} named components · ${count(data.edges.length)} typed relationships · depth ${data.depth}`));
    const candidateCount=data.edges.reduce((total,edge)=>total+(edge.resolution_counts?.candidate || 0),0);
    if(candidateCount) notes.append(element('p',`${count(candidateCount)} candidate calls. Paths through them remain uncertain; inspect their reason and source evidence.`,'caution'));
    if(data.field_targets_grouped) notes.append(element('p','References to this object’s fields are grouped under the selected object; evidence retains the exact field names.'));
    if(data.method_members_grouped) {
      notes.append(element('p',`${count(data.method_members_grouped)} declared methods are grouped under this class. Membership supplies viewing context, never a causal dependency; evidence keeps exact method names.`));
    }
    const ownership=data.ownership_context;
    if(ownership.total || ownership.owner_total) {
      notes.append(element('p',ownership.meaning));
      const members=element('div',undefined,'ownership-members');
      for(const member of [...ownership.owners,...ownership.members]){const button=element('button',`${kindNames[member.component.kind] || member.component.kind} · ${member.component.label}`,'relationship-chip');button.addEventListener('click',()=>focus(member.component.id));members.append(button);}
      notes.append(members);
      if(ownership.truncated || ownership.owners_truncated)notes.append(element('p','First 50 memberships shown per direction; search finds all indexed components.'));
    }
    if(data.filters.hidden_flow_count) notes.append(element('p',`${count(data.filters.hidden_flow_count)} Flows and ${count(data.filters.hidden_relationship_count)} relationships hidden by the local status filter.`));
    const clipped=data.truncation;
    if(clipped.nodes_omitted || clipped.relationships_omitted || clipped.depth_boundary_relationships) notes.append(element('p',`${count(clipped.nodes_omitted)} additional nodes and ${count(clipped.relationships_omitted)} relationships omitted by view limits; ${count(clipped.depth_boundary_relationships)} further relationships beyond the selected depth. Narrow the focus or change depth.`,'caution'));
    if(!data.edges.length) notes.append(element('p','No matching relationships in this view. Coverage and status filters can limit the result; this does not prove that no dependencies exist.','caution'));
    draw(data); relationshipList(data); resetEvidence(); renderDiagnostics(data.diagnostic_context);
  } catch(error) { if(request===state.request) showError(error.message); }
  finally { if(request===state.request) $('graph-wrap').classList.remove('loading'); }
}
function draw(data) {
  const svg=$('graph'); svg.replaceChildren(); positions.clear();
  const layers=new Map();
  for(const node of data.nodes) { if(!layers.has(node.layer)) layers.set(node.layer,[]); layers.get(node.layer).push(node); }
  const keys=[...layers.keys()].sort((a,b)=>a-b);
  const width=Math.max(670,keys.length*340+90), rows=Math.max(...[...layers.values()].map(items=>items.length),1);
  const height=Math.max(420,rows*96+100);
  svg.setAttribute('width',width); svg.setAttribute('height',height); svg.setAttribute('viewBox',`0 0 ${width} ${height}`);
  const defs=svgElement('defs');
  for(const [relation,color] of Object.entries(colors)) {
    const marker=svgElement('marker',{id:'arrow-'+relation,viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:6,markerHeight:6,orient:'auto-start-reverse'});
    marker.append(svgElement('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:color})); defs.append(marker);
  }
  svg.append(defs);
  for(const [index,key] of keys.entries()) {
    const nodes=layers.get(key).sort((a,b)=>(a.id===data.focus.id?-1:b.id===data.focus.id?1:a.label.localeCompare(b.label)));
    const x=45+index*340;
    const label=key===0?'SELECTED CONTEXT':key<0?'INCOMING REFERENCES':'OUTGOING REFERENCES';
    svg.append(svgElement('text',{x:x+115,y:30,class:'layer-title'},label));
    const top=Math.max(58,(height-nodes.length*96)/2);
    nodes.forEach((node,row)=>positions.set(node.id,{x,y:top+row*96,w:230,h:66,node}));
  }
  const pairs=new Map();
  for(const edge of data.edges) { const key=edge.source+'|'+edge.target; if(!pairs.has(key)) pairs.set(key,[]); pairs.get(key).push(edge); }
  for(const edge of data.edges) {
    const source=positions.get(edge.source),target=positions.get(edge.target), peers=pairs.get(edge.source+'|'+edge.target);
    const offset=(peers.indexOf(edge)-(peers.length-1)/2)*17;
    const right=target.x>=source.x, sx=source.x+(right?source.w:0), tx=target.x+(right?0:target.w);
    const sy=source.y+source.h/2+offset,ty=target.y+target.h/2+offset;
    let path, lx, ly;
    if(edge.source===edge.target) {
      path=`M ${source.x+source.w-15} ${source.y} C ${source.x+source.w+75} ${source.y-60}, ${source.x+source.w+75} ${source.y+80}, ${source.x+source.w} ${source.y+45}`;
      lx=source.x+source.w+50; ly=source.y-8;
    } else if(source.x===target.x) {
      const arc=source.x+source.w+45+offset;
      path=`M ${source.x+source.w} ${sy} C ${arc} ${sy}, ${arc} ${ty}, ${target.x+target.w} ${ty}`;
      lx=arc; ly=(sy+ty)/2;
    } else {
      const middle=(sx+tx)/2+offset;
      path=`M ${sx} ${sy} C ${middle} ${sy}, ${middle} ${ty}, ${tx} ${ty}`;
      lx=(sx+tx)/2; ly=(sy+ty)/2-5+offset;
    }
    const uncertainty=candidateSummary(edge);
    const group=svgElement('g',{class:'edge-group '+edge.change+(uncertainty?' candidate':''),'data-edge':edge.id,tabindex:0,role:'button','aria-label':`${source.node.label} ${edge.relation} ${target.node.label}${uncertainty?'; '+uncertainty:''}; show evidence`});
    if(uncertainty) group.append(svgElement('title',{},`${uncertainty}. Candidate call: external hierarchy not verified.`));
    group.append(svgElement('path',{class:'edge-hit',d:path}),svgElement('path',{class:'edge-path',d:path,stroke:colors[edge.relation]||colors.references,'marker-end':'url(#arrow-'+edge.relation+')'}));
    group.append(svgElement('text',{x:lx,y:ly,class:'edge-label',fill:colors[edge.relation]||colors.references,'text-anchor':'middle'},edge.relation+(edge.relationship_count>1?' ×'+edge.relationship_count:'')+(uncertainty?' · candidate':'')));
    group.addEventListener('click',()=>selectEdge(edge)); group.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();selectEdge(edge);}});
    svg.append(group);
  }
  for(const {x,y,w,h,node} of positions.values()) {
    const group=svgElement('g',{class:'node '+node.change+(node.id===data.focus.id?' focus':''),transform:`translate(${x},${y})`,tabindex:0,role:'button','aria-label':`${kindNames[node.kind]} ${node.label}; explore component`});
    group.append(svgElement('title',{},node.label),svgElement('rect',{width:w,height:h}));
    group.append(svgElement('text',{x:12,y:16,class:'node-kind'},(kindNames[node.kind]||node.kind).toUpperCase()));
    const label=node.label.length>31?node.label.slice(0,29)+'…':node.label;
    group.append(svgElement('text',{x:12,y:36,class:'node-name'},label));
    const detail=node.local_status?'XML: '+node.local_status:node.definition_status.replaceAll('_',' ');
    group.append(svgElement('text',{x:12,y:53,class:'node-detail'},detail+(node.change==='removed'?' · removed':node.change==='added'?' · added':'')));
    group.addEventListener('click',()=>focus(node.id)); group.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();focus(node.id);}});
    svg.append(group);
  }
  const legend=$('legend'); legend.replaceChildren();
  for(const relation of [...new Set(data.edges.map(edge=>edge.relation))]) {
    const item=element('span',undefined,'legend-item'); const line=element('i',undefined,'legend-line relation-'+relation);
    item.append(line,element('span',relation)); legend.append(item);
  }
  requestAnimationFrame(centerSelection);
}
function centerSelection() {
  const point=positions.get(state.focus); if(!point)return;
  const wrap=$('graph-wrap'); wrap.scrollTo({left:Math.max(0,point.x+point.w/2-wrap.clientWidth/2),top:Math.max(0,point.y+point.h/2-wrap.clientHeight/2),behavior:'smooth'});
}
function relationshipList(data) {
  const container=$('relationship-list');container.replaceChildren();
  const nodes=new Map(data.nodes.map(node=>[node.id,node]));
  for(const edge of data.edges) {
    const uncertainty=candidateSummary(edge);
    const label=`${nodes.get(edge.source).label} → ${edge.relation} → ${nodes.get(edge.target).label}${uncertainty?' · '+uncertainty:''}`;
    const button=element('button',label,'relationship-chip'); button.dataset.edge=edge.id;
    button.addEventListener('click',()=>selectEdge(edge));container.append(button);
  }
}
function resetEvidence() { ++state.evidenceRequest; $('evidence-content').hidden=true; $('evidence-empty').hidden=false; $('evidence-count').hidden=true; }
function diagnosticRows(records) {
  $('diagnostic-rows').replaceChildren();
  for(const item of records) {
    const row=element('li');
    row.append(element('strong',item.code),element('p',item.message));
    const location=item.location || {};
    row.append(element('p',`${location.path || 'No file'}${location.line?':'+location.line:''}${location.locator?' · '+location.locator:''}`,'hint'));
    if(item.reference || item.symbol)row.append(element('p',item.reference || item.symbol,'qualifiers'));
    $('diagnostic-rows').append(row);
  }
}
function renderDiagnostics(context) {
  ++state.diagnosticsRequest;
  $('component-diagnostics').hidden=!context;
  if(!context)return;
  const snapshot=state.meta?.comparison ? (context.snapshot_id===state.meta.comparison.before_snapshot_id?'Before':'After')+' snapshot · ' : '';
  $('diagnostic-summary').textContent=`Source diagnostics · ${snapshot}${count(context.total)} total`;
  $('diagnostic-codes').textContent=Object.entries(context.code_counts).map(([code,total])=>`${code}: ${count(total)}`).join(' · ') || 'No extraction diagnostics in the component source files; this does not prove complete coverage.';
  $('diagnostic-scope').textContent=`Showing ${count(context.returned_count)} relevant records; ${count(context.omitted_count)} other source records available. ${context.interpretation}`;
  diagnosticRows(context.diagnostics);
  $('diagnostic-more').hidden=!context.omitted_count;
  $('diagnostic-more').textContent='All source diagnostics';
  $('diagnostic-more').dataset.offset='0';
}
async function showDiagnosticsPage() {
  const component=state.focus, request=++state.diagnosticsRequest;
  try {
    const data=await api('/api/diagnostics',{component,offset:Number($('diagnostic-more').dataset.offset)});
    if(component!==state.focus || request!==state.diagnosticsRequest)return;
    diagnosticRows(data.results);
    $('diagnostic-scope').textContent=`Showing ${data.total?data.offset+1:0}–${data.offset+data.results.length} of ${count(data.total)} source diagnostics. File scope can include sibling methods.`;
    $('diagnostic-more').hidden=data.next_offset===null;
    $('diagnostic-more').textContent='Next diagnostics page →';
    $('diagnostic-more').dataset.offset=data.next_offset===null?'':String(data.next_offset);
  }catch(error){if(component===state.focus && request===state.diagnosticsRequest)showError(error.message);}
}
async function selectEdge(edge) {
  state.edge=edge;state.evidenceOffset=0;state.evidenceSnapshot=edge.change==='removed'?'before':'after';
  document.querySelectorAll('[data-edge]').forEach(node=>node.classList.toggle('selected',node.dataset.edge===edge.id));
  $('evidence-empty').hidden=true; $('evidence-content').hidden=false; $('evidence-count').hidden=false;
  const uncertainty=candidateSummary(edge);
  $('evidence-count').textContent=`${count(edge.evidence_count)} source occurrences${uncertainty?' · '+uncertainty:''}`;
  const choices=$('canonical-relation'); choices.replaceChildren();
  for(const relation of edge.relationships) choices.append(new Option(`${relation.source_label} · ${edge.relation} · ${relation.target_label}${relation.resolution?.status==='candidate'?' · candidate':''}`,relation.id));
  $('evidence-snapshot').hidden=edge.change!=='evidence_changed';
  await showEvidence();
}
async function showEvidence() {
  const edge=state.edge;if(!edge)return;
  const id=$('canonical-relation').value;
  const snapshot=state.evidenceSnapshot, offset=state.evidenceOffset, request=++state.evidenceRequest;
  const current=()=>request===state.evidenceRequest && state.edge===edge && $('canonical-relation').value===id;
  try {
    const data=await api('/api/evidence',{relationship:id,snapshot,offset});
    if(!current())return;
    const qualifiers=Object.entries(data.relationship.qualifiers||{}).map(([key,value])=>`${key.replaceAll('_',' ')}: ${typeof value==='object'?JSON.stringify(value):value}`).join(' · ');
    const uncertainty=candidateDescription(data.resolution);
    $('qualifiers').textContent=(state.meta.comparison?`${snapshot==='before'?'Before':'After'} snapshot · `:'')+(uncertainty?uncertainty+' · ':'')+(qualifiers||'Explicit source relationship; no additional qualifiers.');
    $('evidence-rows').replaceChildren();
    for(const occurrence of data.results) {
      const row=element('tr');
       const locator=element('td',occurrence.locator||'Unavailable');
       for(const [key,label] of [['dispatch_site','Dispatch'],['class_literal','Domain literal'],['construct_site','Factory construction'],['constructor','Domain constructor'],['hook','Hook']]) {
         const site=occurrence.resolution?.[key];
         if(typeof site?.path==='string') locator.append(element('div',`${label} ${site.path}:${site.line}`));
       }
       row.append(element('td',occurrence.path),element('td',occurrence.line==null?'Unavailable':String(occurrence.line)),locator,element('td',occurrence.rule));
      $('evidence-rows').append(row);
    }
    $('evidence-page').textContent=`Showing ${data.total?data.offset+1:0}–${data.offset+data.results.length} of ${count(data.total)} occurrences. Relative paths refer to the analyzed source.`;
    $('evidence-more').hidden=data.next_offset===null; $('evidence-more').dataset.offset=data.next_offset===null?'':String(data.next_offset);
    $('evidence-snapshot').textContent=snapshot==='after'?'Show before evidence':'Show after evidence';
  }catch(error){if(current())showError(error.message);}
}
function metadata(meta) {
  state.meta=meta;
  $('revision').textContent=shortId(meta.source_revision)+(meta.dirty?' · working-tree changes':'');
  $('component-count').textContent=count(meta.component_count);
  $('coverage-label').textContent=`Coverage: ${meta.coverage.status} ▾`;
  const details=$('coverage-details');details.replaceChildren();
  details.append(element('p',`${count(meta.coverage.analyzed_files)} files analyzed. ${count(meta.coverage.unsupported_inventory_files)} other source files are inventoried but not analyzed.`));
  const activity=meta.coverage.activity_policy;
  details.append(element('p',activity?.mode==='exclude_explicitly_inactive'
    ? `Local inactive components excluded: ${count(activity.excluded_component_count)}. Unknown or ambiguous status stays included.`
    : 'This snapshot includes all local activation states.'));
  const notes=Object.entries(meta.coverage.resolution_notes).map(([key,value])=>`${count(value)} ${key}`).join(' · ');
  if(notes)details.append(element('p','Extraction notes: '+notes));
  if(meta.coverage.untested_api_versions.length)details.append(element('p','Untested Flow API versions: '+meta.coverage.untested_api_versions.join(', ')));
  details.append(element('p',`Extractor ${meta.extractor_version} · snapshot ${shortId(meta.snapshot_id)}. ${meta.freshness_note}`));
  for(const limitation of meta.coverage.limitations)details.append(element('p',limitation));
  for(const status of meta.flow_statuses)$('status').append(new Option(status,status));
  if(meta.comparison){
    const box=$('comparison');box.hidden=false;box.replaceChildren(element('strong','Static snapshot comparison  '));
    const labels={added:'added relationships',removed:'removed relationships',evidence_changed:'relationships with changed evidence',unchanged:'unchanged relationships'};
    for(const [key,value]of Object.entries(meta.comparison.relationship_changes))box.append(element('span',`${count(value)} ${labels[key]||key}`));
    const comparison=meta.comparison, old=comparison.before, current=comparison.after;
    box.append(element('p',`Before: extractor ${old.extractor_version || 'unknown'}, schema ${old.schema_version}, revision ${shortId(old.source_revision)}. After: extractor ${current.extractor_version || 'unknown'}, schema ${current.schema_version}, revision ${shortId(current.source_revision)}.`));
    box.append(element('p',`Source input hashes: ${count(comparison.inputs.added)} added files, ${count(comparison.inputs.removed)} removed files, ${count(comparison.inputs.changed)} changed files. Scope ${comparison.scope_changed_keys.length?'changed ('+comparison.scope_changed_keys.join(', ')+')':'unchanged'}.`));
    if(comparison.extraction_changed)box.append(element('p','Extraction settings, activity policy or coverage changed. Facts can be added or removed even when source files did not change.','caution'));
    box.append(element('p','Green outlines mark added index facts; dashed red outlines mark removed facts. Amber nodes mark changed source hashes; blue dotted nodes mark index metadata changes. This comparison does not establish a deployed change.'));
  }
}
$('search').addEventListener('input',event=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>search(event.target.value),180);});
document.querySelectorAll('[data-example]').forEach(button=>button.addEventListener('click',()=>{$('search').value=button.dataset.example;search(button.dataset.example);$('search').focus();}));
document.querySelectorAll('[data-mode]').forEach(button=>button.addEventListener('click',()=>{state.mode=button.dataset.mode;document.querySelectorAll('[data-mode]').forEach(item=>{item.classList.toggle('active',item===button);item.setAttribute('aria-pressed',String(item===button));});renderView();}));
$('depth').addEventListener('change',()=>{state.depth=Number($('depth').value);renderView();});
$('status').addEventListener('change',()=>{state.status=$('status').value;renderView();});
$('back').addEventListener('click',()=>{const previous=state.history.pop();if(previous)focus(previous,false);});
$('center').addEventListener('click',centerSelection);
$('canonical-relation').addEventListener('change',()=>{state.evidenceOffset=0;showEvidence();});
$('evidence-more').addEventListener('click',()=>{state.evidenceOffset=Number($('evidence-more').dataset.offset);showEvidence();});
$('diagnostic-more').addEventListener('click',showDiagnosticsPage);
$('evidence-snapshot').addEventListener('click',()=>{state.evidenceSnapshot=state.evidenceSnapshot==='before'?'after':'before';state.evidenceOffset=0;showEvidence();});
api('/api/meta').then(meta=>{
  metadata(meta);
  if(location.hash.length>1) return focus(decodeURIComponent(location.hash.slice(1)));
}).catch(error=>showError(error.message));
