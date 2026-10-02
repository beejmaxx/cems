/* Three views of one unadopted recipe. Exact results come from the local engine. */
'use strict';
window.deskViews = (() => {
  const panel = $('visualPanel');
  let mode = 'subway', state, route, memories = [], reportKey = '';
  let selectedRank = '1', autopsy, selectedExplanation = 0, selectedPiece = 0;
  let heat, heatTemplate, heatBudget = '1b', heatMetric = 'in_budget', heatRow = 0, heatCol = 0, heatCell;
  let busy = false, requestId = 0, currentVersion;
  const cache = new Map();
  const colors = ['#28644a', '#b4693b', '#606ba0', '#7b6289', '#528a91'];
  const views = [
    ['subway', '1 · Subway map', 'Follow a construction from beginning to ending. Select a route to see its choices or change its share of model mass.'],
    ['autopsy', '2 · Autopsy', 'See how a real ranked candidate was constructed, including overlapping explanations and choices shared between pieces.'],
    ['heatmap', '3 · Memory heatmap', 'Compare source spellings within a construction. Counts and first ranks are exact queries of the compiled plan.']
  ];
  const integer = x => BigInt(x).toLocaleString();
  const percent = x => {const n=100*fraction(x);return n>0&&n<.01?'<0.01%':n.toLocaleString(undefined,{maximumFractionDigits:2})+'%'};
  const short = x => {
    const n = Number(x);
    for (const [unit,size] of [['Q',1e15],['T',1e12],['B',1e9],['M',1e6],['K',1e3]])
      if(n>=size)return (n/size).toLocaleString(undefined,{maximumFractionDigits:1})+unit;
    return integer(x);
  };
  const shown = x => x === null ? 'Any letters' : x === '' ? '∅ empty' : x;
  const activeTemplates = () => doc.recipe.templates.filter(isActive);
  const ready = () => state?.visual_views && state.status==='ready' && state.preview_version===version && !dirty && !sending && !saveError;
  const b = (text, fn, cls) => {const button=el('button',text,cls);button.type='button';button.onclick=fn;return button};
  function note(text){return el('p',text,'view-notice')}
  function controlLink(slot){return b('Edit '+label(slot)+' choices',()=>{tab='Words & separators';renderControls();$('controls').scrollIntoView({behavior:'smooth',block:'start'})},'view-action')}
  function slotOptions(slot){const excluded=doc.disabled_options[slot]||[];return (doc.recipe.slots[slot]?.options||[]).filter((_,i)=>!excluded.includes(i))}
  function choiceText(slot,template,index){
    if(!slot)return '';const s=doc.recipe.slots[slot];if(s?.alphabet)return 'any letters';
    let options=slotOptions(slot);
    const widths=name=>doc.recipe.slots[name]?.alphabet?Object.keys(doc.recipe.lengths?.[template.length_table]||{}).map(Number):slotOptions(name).map(o=>o.value.length);
    if(template.lengths==='equal'){
      const admitted=Object.keys(doc.recipe.lengths?.[template.length_table]||{}).map(Number).filter(n=>template.chunks.every(name=>widths(name).includes(n)));
      options=options.filter(o=>admitted.includes(o.value.length));
    }else if(template.lengths==='unequal'&&template.chunks.length===2){
      const other=widths(template.chunks[1-index]);options=options.filter(o=>other.some(n=>n!==o.value.length));
    }
    return options.length?options.map(o=>shown(o.value)).slice(0,3).join(' / '):'no admitted words';
  }
  function setMode(next){mode=next;requestId++;render()}
  function header(){
    $('viewTabs').replaceChildren();
    for(const [key,title] of views){const button=b(title,()=>setMode(key),key===mode?'active':'');button.setAttribute('aria-pressed',key===mode);$('viewTabs').append(button)}
    const v=views.find(v=>v[0]===mode);$('viewHeading').textContent=v[1].slice(4);$('viewDescription').textContent=v[2];
    $('rankForm').hidden=mode!=='autopsy';
  }
  function gate(){
    if(ready())return false;
    panel.append(note(state&&!state.visual_views?'Restart recollect desk to load these new views. Your saved draft will reopen.':'The map follows your editable draft. Exact candidate and heatmap results become available when its preview is ready.'));
    return true;
  }
  function render(){if(!doc)return;header();panel.replaceChildren();if(mode==='subway')renderSubway();else if(!gate()){if(mode==='autopsy')renderAutopsy();else renderHeatmap()}}
  function routeInfo(t){
    const family=t.family||'default', templates=activeTemplates();
    const used=new Set(templates.map(t=>t.family||'default'));
    const families=doc.recipe.families||{default:1};
    const familyTotal=Object.entries(families).reduce((sum,[k,v])=>sum+(used.has(k)?fraction(v):0),0);
    const withinTotal=templates.filter(x=>(x.family||'default')===family).reduce((sum,x)=>sum+fraction(x.weight||1),0);
    const share=isActive(t)?fraction(families[family])/familyTotal*fraction(t.weight||1)/withinTotal:0;
    const relationships=doc.recipe.relationships||{};
    return {family,share,case:t.case||relationships[t.lengths==='equal'?'equal_case':'other_case']||'literal',separators:t.separators||relationships.separators||'shared'};
  }
  function tokens(t){return t.pattern.split(/(\{[^}]+\})/).filter(Boolean).map(p=>{
    const ref=p.startsWith('{')?p.slice(1,-1):null;
    const match=ref?.match(/^chunk(\d+)$/);const slot=match?t.chunks[Number(match[1])-1]:ref==='last_sep'?'separator':ref;
    return {ref,slot,title:match?choiceText(slot,t,Number(match[1])-1):ref==='last_sep'?'final separator?':ref?label(ref):JSON.stringify(p)};
  })}
  function svg(tag,attributes,text){const e=document.createElementNS('http://www.w3.org/2000/svg',tag);Object.entries(attributes||{}).forEach(([k,v])=>e.setAttribute(k,v));if(text!==undefined)e.textContent=text;return e}
  function renderSubway(){
    const templates=[...doc.recipe.templates].sort((a,b)=>routeInfo(b).share-routeInfo(a).share);
    if(!templates.some(t=>t.id===route))route=templates.find(isActive)?.id||templates[0].id;
    panel.append(note('Line width follows each route’s share of the full model, before history exclusions. A minimum width keeps tiny routes clickable. It is not a per-password probability.'));
    const key=el('div',undefined,'route-key');const familyNames=[...new Set(doc.recipe.templates.map(t=>t.family||'default'))];
    familyNames.forEach((f,i)=>{const item=el('span');const line=el('i');line.style.background=colors[i%colors.length];add(item,line,document.createTextNode(label(f)));key.append(item)});panel.append(key);panel.append(el('p','Routes are shown from most to least model mass.','small muted'));
    const scroll=el('div',undefined,'svg-scroll');scroll.style.maxHeight='600px';
    const height=templates.length*88+60;const map=svg('svg',{viewBox:`0 0 1100 ${height}`,class:'subway-svg',role:'group','aria-label':'Weighted construction routes. Select a route to inspect or adjust it.'});
    map.append(svg('path',{d:`M55 26 V${height-45}`,stroke:'#d7e1d4','stroke-width':8,fill:'none'}));
    map.append(svg('text',{x:18,y:16,class:'route-budget'},'Model'));
    templates.forEach((t,i)=>{
      const info=routeInfo(t), y=i*88+65, color=colors[familyNames.indexOf(info.family)%colors.length];
      const g=svg('g',{tabindex:0,role:'button','aria-label':'Select route '+t.id,'aria-pressed':t.id===route});
      g.append(svg('rect',{x:85,y:y-37,width:998,height:78,rx:9,fill:t.id===route?'#f0f4ed':'transparent',class:'route-bg'}));
      const ts=tokens(t), xs=ts.map((_,j)=>360+j*(685/Math.max(1,ts.length-1)));
      const path=`M55 ${y-24} Q55 ${y} 90 ${y} H${xs.at(-1)}`;
      g.append(svg('path',{d:path,stroke:isActive(t)?color:'#c4cac4','stroke-width':Math.max(1.5,22*info.share),fill:'none','stroke-linecap':'round',...(isActive(t)?{}:{'stroke-dasharray':'4 5'})}));
      g.append(svg('text',{x:102,y:y-16,class:'route-label'},label(t.id)));
      g.append(svg('text',{x:102,y:y+23,class:'route-budget'},isActive(t)?percent(info.share)+' of model mass':'Excluded from this draft'));
      ts.forEach((token,j)=>{
        const x=xs[j];g.append(svg('circle',{cx:x,cy:y,r:5.5,stroke:color,class:'route-station'}));
        const title=token.title.length>16?token.title.slice(0,14)+'…':token.title;
        const text=svg('text',{x,y:y+23,'text-anchor':'middle',class:'route-budget'},title);text.append(svg('title',{},token.title));g.append(text);
        if(token.ref?.startsWith('chunk'))g.append(svg('text',{x,y:y-14,'text-anchor':'middle',class:'route-budget'},token.slot));
      });
      g.append(svg('path',{d:path,class:'route-hit'}));
      g.onclick=()=>{route=t.id;renderSubwaySelection();map.querySelectorAll('[aria-pressed]').forEach(n=>{n.setAttribute('aria-pressed',n===g);n.querySelector('.route-bg').setAttribute('fill',n===g?'#f0f4ed':'transparent')});document.getElementById('routeDetail').scrollIntoView({behavior:'smooth',block:'nearest'})};
      g.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();route=t.id;render()}};
      map.append(g);
    });
    scroll.append(map);panel.append(scroll);panel.append(el('div',undefined,'route-detail-container'));renderSubwaySelection();
    panel.append(note('This is the current recipe’s structure. Historical experiments not translated into it do not appear as routes. “Memories & missing ideas” records those gaps; the seed count is not a confidence score.'));
  }
  function renderSubwaySelection(){
    const container=panel.querySelector('.route-detail-container');if(!container)return;
    container.replaceChildren();const t=doc.recipe.templates.find(t=>t.id===route);if(!t)return;
    const info=routeInfo(t), c=card(label(t.id));c.id='routeDetail';c.classList.add('route-detail');
    c.append(el('div',percent(info.share)+' of the model · '+label(info.family),'badge'));
    const path=el('div',undefined,'pattern');tokens(t).forEach((token,i)=>{if(i)path.append(el('span','→','muted'));const station=el('span',token.title,'pill');station.title=token.slot||'Literal text';path.append(station)});c.append(path);
    const summary=el('div',undefined,'route-summary');for(const x of [label(t.lengths||'any')+' source lengths',label(info.case)+' case',info.separators==='shared'?'Matching separators':'Independent separators'])summary.append(el('span',x,'badge'));c.append(summary);
    const controls=el('div',undefined,'route-tune');
    const tune=factor=>changed(()=>t.weight=scaleExact(t.weight||1,factor));
    const less=b('½ Less weight',()=>tune([1n,2n]));const more=b('2× More weight',()=>tune([2n,1n]));less.disabled=more.disabled=!isActive(t);add(controls,less,more,b(isActive(t)?'Exclude route':'Include route',()=>changed(()=>doc.disabled_templates=isActive(t)?[...doc.disabled_templates,t.id]:doc.disabled_templates.filter(id=>id!==t.id))));c.append(controls);
    c.append(el('p',`Relative weight ${t.weight||1} within ${label(info.family)}. These buttons redistribute that family’s mass across its enabled routes. Family controls change the total family budget.`,'small muted'));
    if((t.chunks||[]).length===2){c.append(b('Add swapped alternative · split 1:1',()=>{
      changed(()=>{const twin=clone(t);let id=t.id+'-swapped',n=2;while(doc.recipe.templates.some(x=>x.id===id))id=t.id+'-swapped-'+n++;
        twin.id=id;twin.pattern=t.pattern.replaceAll('{chunk1}','{temporary}').replaceAll('{chunk2}','{chunk1}').replaceAll('{temporary}','{chunk2}');
        t.weight=scaleExact(t.weight||1,[1n,2n]);twin.weight=t.weight;doc.recipe.templates.push(twin);route=id;});
    },'view-action'));c.append(el('p','Adds a second order and splits this route’s existing weight equally. The 1:1 split is a reversible experiment, not an inferred memory.','small muted'))}
    const links=el('div',undefined,'toolbar');[...new Set(t.chunks||[])].forEach(slot=>links.append(controlLink(slot)));c.append(links);container.append(c);
  }
  function scaleExact(value,[n,d]){
    let s=String(value), a,z;
    if(s.includes('/'))[a,z]=s.split('/').map(BigInt);else if(s.includes('.')){const p=s.split('.');a=BigInt(p.join(''));z=10n**BigInt(p[1].length)}else{a=BigInt(s);z=1n}
    a*=n;z*=d;let x=a,y=z;while(y){[x,y]=[y,x%y]}a/=x;z/=x;return z===1n?String(a):a+'/'+z;
  }
  async function request(kind,params,onDone){
    if(busy)return;
    const key=JSON.stringify([version,kind,params]);if(cache.has(key)){onDone(cache.get(key));return}
    busy=true;const id=++requestId;const requestedVersion=version;
    try{const result=await api('/api/view',{kind,version,...params});if(id!==requestId||version!==requestedVersion||!ready())return;cache.set(key,result);onDone(result)}
    catch(e){if(id===requestId){const error=el('div',undefined,'view-error');add(error,el('p',e.message),b('Try again',()=>{busy=false;render()}));panel.querySelector('.view-wait')?.replaceWith(error)}}
    finally{busy=false;if(id!==requestId&&ready()&&mode!=='subway')render()}
  }
  function candidateRows(){
    const result=[],seen=new Set();for(const r of [...(state.report?.top||[]),...(state.report?.windows||[]).flatMap(w=>w.rows||[])])if(!seen.has(r.rank)){seen.add(r.rank);result.push(r)}return result;
  }
  function renderAutopsy(){
    const rows=candidateRows();
    const toolbar=el('form',undefined,'toolbar');const rankInput=el('input');rankInput.value=selectedRank;rankInput.setAttribute('aria-label','Autopsy rank');rankInput.placeholder='1, 1t, 100t…';
    const submit=el('button','Explain rank');submit.type='submit';add(toolbar,rankInput,submit);toolbar.onsubmit=e=>{e.preventDefault();requestId++;selectedRank=rankInput.value.trim();autopsy=null;selectedExplanation=selectedPiece=0;render()};panel.append(toolbar);
    panel.append(el('p','Choose one of the top ten or any rank. Counts below use the current compiled '+state.report.view+' plan.','small muted'));
    const outside=(state.report?.windows||[]).filter(w=>w.outside_range);if(outside.length)panel.append(el('p','Requested marks outside this plan: '+outside.map(w=>integer(w.rank)).join(', ')+'.','small muted'));
    const grid=el('div',undefined,'audit-grid'),list=el('div',undefined,'candidate-list'),detail=el('div');
    rows.forEach(row=>{const item=b('',()=>{requestId++;selectedRank=row.rank;autopsy=null;selectedExplanation=selectedPiece=0;render()},'candidate-pick'+(row.rank===selectedRank?' active':''));add(item,el('span','#'+integer(row.rank),'rank'),el('code',row.text));list.append(item)});add(grid,list,detail);panel.append(grid);
    if(!autopsy||autopsy.version!==version||autopsy.rank!==selectedRank){detail.append(el('div','Tracing the exact construction…','view-wait'));request('autopsy',{rank:selectedRank},result=>{autopsy=result;selectedRank=result.rank;render()});return}
    add(detail,el('div','#'+integer(autopsy.rank),'eyebrow'),el('div',autopsy.text,'autopsy-string'));
    detail.append(el('p','Model score '+autopsy.score,'fraction'));
    const previous=[...(state.previous?.top||[]),...(state.previous?.windows||[]).flatMap(w=>w.rows||[])].find(r=>r.rank===autopsy.rank);if(previous&&previous.hex!==autopsy.hex)detail.append(el('p','Previously at this rank: '+previous.text,'before'));
    const contribution=autopsy.contributions[selectedExplanation]||autopsy.contributions[0];
    const trace=contribution.trace;
    detail.append(el('p','One compatible construction from '+label(contribution.evidence.template)+'. Select a piece to see its source.','small muted'));
    const pieces=el('div',undefined,'piece-strip');
    trace.pieces.forEach((piece,i)=>{const chip=b('',()=>{selectedPiece=i;render()},'piece');const kind=piece.role==='prefix'?0:piece.role==='chunk1'?1:piece.role==='chunk2'?2:piece.role==='suffix'?3:4;chip.style.setProperty('--piece-color',colors[kind]);chip.style.setProperty('--piece-bg',['#e7f0e6','#faeee3','#eceefa','#f2eaf3','#e9f2f1'][kind]);add(chip,el('code',piece.text||'∅'),el('small',piece.role==='last_sep'?'last separator':piece.slot||piece.role));chip.setAttribute('aria-pressed',i===selectedPiece);pieces.append(chip)});detail.append(pieces);
    const piece=trace.pieces[Math.min(selectedPiece,trace.pieces.length-1)];
    if(piece){const p=el('div',undefined,'piece-detail');p.append(el('strong',(piece.slot?label(piece.slot):'Literal text')+': '+JSON.stringify(piece.text)));
      for(const fact of piece.facts){
        if('source' in fact)p.append(el('p',fact.source===null?`Uniform ${fact.width}-letter draw from ${fact.alphabet}.`:'Listed source: '+JSON.stringify(fact.source)+'.','small'));
        if(fact.style)p.append(el('p','Case: '+(Array.isArray(fact.style)?fact.style[1]+' (l = lower, u = upper)':label(fact.style))+'.','small'));
        if(fact.transform&&fact.transform!=='identity')p.append(el('p','Transformation: '+label(fact.transform)+'.','small'));
        if(fact.conditional_weight){const decision='source' in fact.choice?'Source choice within the admitted spellings':fact.choice.transform?'This transformation':'This deletion position';p.append(el('p',decision+': '+percent(fact.conditional_weight)+' ('+fact.conditional_weight+').','small'))}
        if(fact.deletion_position)p.append(el('p','Delete source position '+fact.deletion_position+'.','small'));
      }
      if(piece.facts.some(f=>f.transform==='identity'))p.append(el('p','No edit operation on this path. A short listed spelling is not automatically a typing error.','small muted'));
      if(!piece.facts.length)p.append(el('p','Determined by the construction or its shared choices below.','small muted'));
      if(piece.slot&&doc.recipe.slots[piece.slot])p.append(controlLink(piece.slot));detail.append(p);
    }
    if(trace.contexts.length){const shared=el('div',undefined,'score-details');shared.append(el('strong','Linked choices on this path'));
      trace.contexts.forEach(c=>{const line=el('p',undefined,'small');line.append(el('span',c.scope+': '));Object.entries(c.values).forEach(([key,value])=>line.append(el('span',key+' = '+JSON.stringify(value),'choice-chip')));shared.append(line)});
      shared.append(el('p','A shared separator or case pattern is chosen once and reused. The pieces are not independent confidence percentages.','small muted'));detail.append(shared);
    }
    if(trace.other_paths)detail.append(note('This branch has other compatible construction paths, too. The colored breakdown shows one; the branch score includes them all.'));
    const exact=el('details',undefined,'score-details');exact.append(el('summary','Exact score accounting'));
    if(contribution.selection){const selection=contribution.selection;exact.append(el('p',label(selection.family)+': '+percent(selection.family_mass)+' family mass × '+percent(selection.template_given_family)+' for this construction × '+percent(selection.branch_given_template)+' for its length branch.','small'))}
    add(exact,el('p',`Normalized branch weight ${contribution.branch_weight} × conditional string probability ${contribution.conditional_probability} = ${contribution.contribution}.`,'fraction'),el('p',`Displayed construction path: ${trace.shown_path_probability} conditional probability. Whole branch: ${trace.probability}.`,'fraction'));detail.append(exact);
    detail.append(el('h3',autopsy.contributions.length+' contributing explanation'+(autopsy.contributions.length===1?'':'s')));
    detail.append(el('p','Shares below are portions of this candidate’s model score. They are not confidence in your recollection.','small muted'));
    autopsy.contributions.forEach((c,i)=>{const item=b('',()=>{selectedExplanation=i;selectedPiece=0;render()},'contribution-choice'+(selectedExplanation===i?' active':''));add(item,el('strong',label(c.evidence.template)+' · '+percent(c.score_share)),el('div',c.hypothesis,'small muted'));const share=el('div',undefined,'share');const fill=el('span');fill.style.width=(100*fraction(c.score_share))+'%';fill.style.background=colors[i%colors.length];share.append(fill);item.append(share);item.append(el('div',c.contribution,'fraction'));detail.append(item)});
    const related=memories.filter(m=>m.controls.some(c=>trace.pieces.some(p=>c.kind==='slot'&&c.key===p.slot)));
    if(related.length){const evidence=el('details',undefined,'score-details');evidence.append(el('summary','Relevant recollections & gaps'));related.forEach(m=>{add(evidence,el('strong',m.title),el('p',m.record,'small'),el('p','Gap: '+m.gap,'small muted'))});detail.append(evidence)}
  }
  function renderHeatmap(){
    const templates=activeTemplates().filter(t=>(t.chunks||[]).length>=2);
    if(!templates.length){panel.append(note('This recipe needs at least two chunks for a heatmap.'));return}
    if(!templates.some(t=>t.id===heatTemplate))heatTemplate=templates.find(t=>t.id==='two-recorded')?.id||templates[0].id;
    const controls=el('form',undefined,'heat-controls');
    const templateLabel=el('label','Construction'),templateSelect=el('select');templateSelect.setAttribute('aria-label','Heatmap construction');templates.forEach(t=>{const option=el('option',label(t.id));option.value=t.id;templateSelect.append(option)});templateSelect.value=heatTemplate;
    templateSelect.onchange=()=>{requestId++;heatTemplate=templateSelect.value;heatRow=heatCol=0;heat=null;heatCell=null;render()};templateLabel.append(templateSelect);
    const budgetLabel=el('label','Search budget / rank ceiling'),budget=el('input');budget.value=heatBudget;budget.setAttribute('aria-label','Heatmap budget');budgetLabel.append(budget);
    const update=el('button','Calculate');update.type='submit';controls.onsubmit=e=>{e.preventDefault();requestId++;heatBudget=budget.value.trim();heat=null;heatCell=null;render()};
    const metricLabel=el('label','Color cells by'),metric=el('select');metric.setAttribute('aria-label','Heatmap color metric');[['in_budget','Count inside budget'],['first_rank','First global rank'],['count','All remaining candidates']].forEach(([v,l])=>{const o=el('option',l);o.value=v;metric.append(o)});metric.value=heatMetric;metric.onchange=()=>{heatMetric=metric.value;render()};metricLabel.append(metric);
    add(controls,templateLabel,budgetLabel,update,metricLabel);panel.append(controls);
    panel.append(note('Axes are source spellings before case and edit operations. Each cell includes candidates that this pair can produce in the selected construction. Ambiguous candidates can belong to more than one cell; do not add the cells as a unique total.'));
    if(!heat||heat.version!==version){panel.append(el('div','Counting exact intersections with the ranked plan…','view-wait'));request('heatmap',{template:heatTemplate,budget:heatBudget,row_start:heatRow,col_start:heatCol},result=>{heat=result;render()});return}
    panel.append(el('p',`Rows: ${label(heat.row_slot)} · Columns: ${label(heat.column_slot)} · Global ranks 1–${integer(heat.budget)}${heat.budget!==heat.requested_budget?' (capped at plan size)':''}. Other parts vary according to this construction.`,'small muted'));
    const legend=el('div',undefined,'heat-legend');add(legend,el('span',heatMetric==='first_rank'?'Later':'Fewer'),el('span',undefined,'legend-ramp'),el('span',heatMetric==='first_rank'?'Earlier':'More'),el('span','Log scale within this page; gray = no match'));panel.append(legend);
    const scroll=el('div',undefined,'heat-scroll'),table=el('table',undefined,'heat-table'),head=el('thead'),tr=el('tr');tr.append(el('th',label(heat.row_slot)+' ↓ / '+label(heat.column_slot)+' →'));heat.columns.forEach(c=>tr.append(el('th',shown(c))));head.append(tr);table.append(head);
    const positive=heat.cells.filter(c=>c[heatMetric]!==null&&Number(c[heatMetric])>0).map(c=>Math.log10(Number(c[heatMetric])));
    const lo=Math.min(...positive),hi=Math.max(...positive),body=el('tbody');
    heat.rows.forEach(row=>{const tr=el('tr');const rh=el('th',shown(row));rh.scope='row';tr.append(rh);heat.columns.forEach(column=>{
      const cell=heat.cells.find(c=>c.row===row&&c.column===column),td=el('td');
      const value=cell[heatMetric],valid=value!==null&&Number(value)>0;let intensity=valid?(hi===lo?1:(Math.log10(Number(value))-lo)/(hi-lo)):0;if(valid&&heatMetric==='first_rank')intensity=1-intensity;
      const button=b('',()=>{heatCell=cell;renderHeatCell()},'heat-cell');button.style.setProperty('--cell-bg',!valid?'#f0f1ed':`hsl(${155-133*intensity} 33% ${92-43*intensity}%)`);button.style.setProperty('--cell-fg',valid&&intensity>.7?'#fff':'#20342d');
      add(button,el('span',heatMetric==='first_rank'?(value?'#'+short(value):'—'):short(value)),el('small',heatMetric==='first_rank'?'first rank':heatMetric==='in_budget'?'within budget':'candidates'));
      button.title=`${shown(row)} × ${shown(column)}: ${integer(cell.count)} candidates; ${integer(cell.in_budget)} in budget; first rank ${cell.first_rank?integer(cell.first_rank):'none'}`;
      button.setAttribute('aria-label',button.title);td.append(button);tr.append(td);
    });body.append(tr)});table.append(body);scroll.append(table);panel.append(scroll);
    const pages=el('div',undefined,'toolbar');
    for(const [axis,start,total] of [['rows',heatRow,heat.row_total],['columns',heatCol,heat.column_total]]){
      const set=n=>{requestId++;if(axis==='rows')heatRow=n;else heatCol=n;heat=null;heatCell=null;render()};
      const prev=b('← '+axis,()=>set(Math.max(0,start-8))),next=b(axis+' →',()=>set(start+8));prev.disabled=start===0;next.disabled=start+8>=total;
      add(pages,prev,el('span',`${start+1}–${Math.min(start+8,total)} / ${total}`,'small muted'),next);
    }panel.append(pages);panel.append(el('div',undefined,'heat-detail-container'));renderHeatCell();
  }
  function renderHeatCell(){
    const container=panel.querySelector('.heat-detail-container');if(!container)return;container.replaceChildren();
    const cell=heatCell||heat.cells.find(c=>Number(c.in_budget)>0)||heat.cells.find(c=>c.first_rank)||heat.cells[0];
    const c=card(shown(cell.row)+' × '+shown(cell.column));
    const numbers=el('div',undefined,'numeric-readout');for(const [value,title] of [[integer(cell.count),'candidates in the whole plan'],[integer(cell.in_budget),'inside this budget'],[cell.first_rank?'#'+integer(cell.first_rank):'No match','first global rank']]){const n=el('div');add(n,el('strong',value),el('small',title));numbers.append(n)}c.append(numbers);
    if(cell.first_rank)c.append(b('Explain its first candidate',()=>{selectedRank=cell.first_rank;autopsy=null;selectedExplanation=selectedPiece=0;setMode('autopsy')},'view-action'));
    c.append(el('p','Membership means this pair is one valid explanation. The first candidate may also receive score from other constructions.','small muted'));container.append(c);
  }
  async function loadMemories(){
    try{const result=await api('/api/memories');memories=result.mapping.cards;const target=$('memoryCards');target.replaceChildren();
      target.append(el('p','Evidence links describe the starting recipe. They do not assign probability, and edits may change which controls represent an idea.','small muted'));
      if(!memories.length)target.append(el('p',result.mapping.message||'No personal evidence map supplied.'));
      memories.forEach(m=>{const c=el('details',undefined,'memory-card');c.append(el('summary',m.title));add(c,el('span',m.status||m.kind,'badge'),el('p',m.record),el('p','In the starting recipe: '+m.model),el('p','Still open: '+m.gap,'notice'),el('p','Source references: '+m.sources.join('; '),'small muted'));target.append(c)});
    }catch(e){$('memoryCards').textContent=e.message}
  }
  loadMemories();
  return {
    controlsReady(){if(!state)render()},
    draftChanged(){if(!doc)return;requestId++;cache.clear();autopsy=null;heat=null;if(mode==='subway'){header();panel.replaceChildren();renderSubway()}else{panel.classList.add('pending')}},
    status(s){state=s;panel.classList.toggle('pending',mode!=='subway'&&!ready());const becameReady=ready()&&currentVersion!==version;if(becameReady){currentVersion=version;render()}else if(!s.visual_views)render()},
    report(s){state=s;const key=s.report?.plan_sha256;if(key&&key!==reportKey){reportKey=key;cache.clear();autopsy=null;heat=null;requestId++;render()}},
  };
})();
