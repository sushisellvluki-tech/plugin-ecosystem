'use strict';
const $ = id => document.getElementById(id);
const state = {token: '', session: null, epoch: 0, controller: new AbortController(), keys: new Map(),
  page: [], cursor: null, batch: null, run: null, files: [], busy: false};
let disabledBefore = new Map();
const titles = {ingested:'Принят',blocked_ingestion:'Ошибка приёма',needs_review:'Нужна проверка',
  failed:'Есть замечания',ready:'Готов к публикации',published:'Опубликован',accepted:'Принят',
  duplicate:'Дубль',rejected:'Отклонён',pass:'Пройдено',fail:'Замечание'};
const checkNames = ['Полнота пакета','Точность и полнота фактов','Согласованность с историей','Итоговое подтверждение'];
function node(tag, text, cls) { const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el; }
function show(id, yes=true) { $(id).hidden=!yes; }
function notice(text, error=false) { $('notice').textContent=text;$('notice').className=error?'error':'';show('notice',Boolean(text)); }
function can(action) { return state.session?.tools.includes('meetings__'+action); }
function badge(status, stale=false) { return node('span',stale?'Устарело':(titles[status]||status),
  'badge '+(stale||['needs_review','ready'].includes(status)?'warn':['fail','failed','rejected','blocked_ingestion'].includes(status)?'bad':['pass','published'].includes(status)?'good':'')); }
function setBusy(value) {
  state.busy=value;
  if(value) {disabledBefore=new Map();document.querySelectorAll('button,input,textarea,select').forEach(el=>{if(el.id!=='logout'){disabledBefore.set(el,el.disabled);el.disabled=true;}});}
  else {for(const [el,disabled] of disabledBefore)if(el.isConnected)el.disabled=disabled;disabledBefore.clear();}
}
async function perform(action) {
  if(state.busy)return;
  const epoch=state.epoch;setBusy(true);notice('');
  try { await action(); } catch(error) {if(epoch===state.epoch&&error.name!=='AbortError')notice(error.message||'Не удалось выполнить действие.',true);}
  finally {if(epoch===state.epoch)setBusy(false);}
}
document.addEventListener('click', event=>{if(state.busy&&event.target.closest('button')?.id!=='logout'){event.preventDefault();event.stopImmediatePropagation();}},true);
function reset() {
  state.epoch++;state.controller.abort();state.controller=new AbortController();state.token='';state.session=null;
  state.keys.clear();state.page=[];state.cursor=null;state.batch=null;state.run=null;state.files=[];setBusy(false);
  ['batch-list','sources','saved-claims','checks','claim-editor','file-list','history-content'].forEach(id=>$(id).replaceChildren());
  ['identity','batch-title','batch-id','review-message'].forEach(id=>$(id).textContent='');
  document.querySelectorAll('form').forEach(form=>form.reset());document.querySelectorAll('dialog').forEach(d=>d.close());
  show('workspace',false);show('logout',false);show('login');show('detail',false);show('upload-panel',false);notice('');
}
async function request(path, options={}) {
  const epoch=state.epoch;
  let response;
  try {response=await fetch(path,{...options,credentials:'omit',cache:'no-store',signal:state.controller.signal,
    headers:{'Authorization':'Bearer '+state.token,...options.headers}});}
  catch(error) {if(error.name==='AbortError')throw error;throw Error('Связь прервалась. Повторите действие: ключ повтора сохранён в этой вкладке.');}
  if(epoch!==state.epoch)throw new DOMException('Session changed','AbortError');
  if(response.status===401){reset();notice('Ключ недействителен или срок его действия истёк. Войдите снова.',true);throw new DOMException('Signed out','AbortError');}
  let body;try{body=await response.json();}catch{throw Error('Сервер вернул непонятный ответ. Повторите запрос.');}
  if(epoch!==state.epoch)throw new DOMException('Session changed','AbortError');
  return {response,body};
}
async function invoke(action,args,write=false) {
  const fingerprint=JSON.stringify([action,args]);
  if(write&&!state.keys.has(fingerprint))state.keys.set(fingerprint,crypto.randomUUID());
  const headers={'Content-Type':'application/json'};if(write)headers['Idempotency-Key']=state.keys.get(fingerprint);
  const {response,body}=await request('/meetings/v1/tools/invoke',{method:'POST',headers,body:JSON.stringify({name:'meetings__'+action,arguments:args})});
  if(!response.ok||!body.ok){
    if(write&&response.status<500&&response.status!==429)state.keys.delete(fingerprint);
    const code=body.error?.code;
    const messages={FORBIDDEN:'Недостаточно прав или требуется другой проверяющий.',NOT_FOUND:'Запись недоступна в вашей организации.',
      CONFLICT:'Версия изменилась или условия проверки не выполнены. Обновите пакет и проверьте замечания.',
      DEPENDENCY_UNAVAILABLE:'Сервис временно занят. Повторите действие — ключ повтора сохранён.',
      NOT_IMPLEMENTED:'Для этого действия нужна подключённая PostgreSQL.'};
    throw Error(messages[code]||body.error?.message||'Запрос не выполнен. Проверьте данные и попробуйте снова.');
  }
  if(write)state.keys.delete(fingerprint);
  return body.data;
}
$('login-form').addEventListener('submit',event=>{event.preventDefault();const token=$('token').value.trim();perform(async()=>{
  state.token=token;$('token').value='';
  try {const {response,body}=await request('/meetings/v1/session');if(!response.ok||!body.tools?.includes('meetings__list_batches'))throw Error('Для кабинета нужны права чтения пакетов meetings.');state.session=body;
    $('identity').textContent=body.organization_id+' / '+body.actor_id;show('login',false);show('workspace');show('logout');show('welcome');show('new-batch',can('import_batch'));await loadList();}
  catch(error){if(!state.session)state.token='';throw error;}
});});
$('logout').addEventListener('click',reset);
window.addEventListener('pagehide',reset);
async function loadList(append=false) {
  const data=await invoke('list_batches',{cursor:append?state.cursor:null,limit:20});
  state.page=append?[...state.page,...data.records]:data.records;state.cursor=data.next_cursor;
  const list=$('batch-list');list.replaceChildren();
  state.page.forEach(row=>{const button=node('button',undefined,'batch-row'+(row.batch_id===state.batch?.batch_id?' active':''));
    button.append(node('strong',row.source_account_id),node('small',new Date(row.created_at).toLocaleString('ru-RU')+' · '+row.files+' записей'),badge(row.run_status||row.ingestion_status,row.stale));
    button.addEventListener('click',()=>perform(()=>openBatch(row)));list.append(button);});
  $('page-count').textContent='Показано пакетов: '+state.page.length;show('empty-list',state.page.length===0);show('more',Boolean(state.cursor));
}
async function openBatch(row) {
  const batch=await invoke('get_batch',{batch_id:row.batch_id});
  const run=row.run_id?await invoke('get_run',{run_id:row.run_id}):null;
  state.batch=batch;state.run=run;renderDetail();
}
$('refresh').addEventListener('click',()=>perform(async()=>{await loadList();if(state.batch){const row=state.page.find(r=>r.batch_id===state.batch.batch_id);if(row)await openBatch(row);else if(state.run){state.run=await invoke('get_run',{run_id:state.run.run_id});renderDetail();}}}));
$('more').addEventListener('click',()=>perform(()=>loadList(true)));
$('new-batch').addEventListener('click',()=>{show('welcome',false);show('detail',false);show('upload-panel');$('account').focus();});
$('close-upload').addEventListener('click',()=>{show('upload-panel',false);show(state.batch?'detail':'welcome');});
$('files').addEventListener('change',()=>perform(async()=>{
  const epoch=state.epoch;
  state.files=[];$('file-list').replaceChildren();
  const files=Array.from($('files').files);if(files.length>20)throw Error('Можно выбрать не более 20 файлов.');
  const errors=[];
  for(const file of files){try{if(!file.name.toLowerCase().endsWith('.txt'))throw Error('нужен TXT');if(file.size>32768)throw Error('больше 32 КиБ');
    const buffer=await file.arrayBuffer();if(epoch!==state.epoch)throw new DOMException('Session changed','AbortError');
    const text=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(buffer);if(!text.trim()||text.includes('\0'))throw Error('пустой текст или недопустимый символ');
    state.files.push({filename:file.name,format:'text/plain',text,external_id:null});
  }catch(error){if(error.name==='AbortError')throw error;errors.push(file.name+': '+error.message);}}
  if(errors.length){state.files=[];$('files').value='';throw Error(errors.join('; '));}
  state.files.forEach((file,index)=>{const row=node('div',undefined,'file-row');row.append(node('strong',file.filename));const label=node('label','ID записи, если известен');const input=node('input');input.placeholder='Необязательно';input.maxLength=256;input.id='file-id-'+index;label.htmlFor=input.id;input.addEventListener('input',()=>file.external_id=input.value.trim()||null);row.append(label,input);$('file-list').append(row);});
}));
$('upload-form').addEventListener('submit',event=>{event.preventDefault();perform(async()=>{
  const sources=state.files.map(f=>({...f}));const pasted=$('paste-text').value;
  if(pasted.trim())sources.push({filename:$('paste-name').value.trim()||'Вставленный текст.txt',format:'text/plain',text:pasted,external_id:$('paste-id').value.trim()||null});
  if(!sources.length||sources.length>20)throw Error('Добавьте от 1 до 20 текстовых записей.');
  if(sources.reduce((n,s)=>n+new TextEncoder().encode(s.text).length,0)>32768)throw Error('Суммарный текст превышает 32 КиБ. Разделите пакет.');
  const batch=await invoke('import_batch',{source_account_id:$('account').value.trim(),sources},true);
  state.batch=batch;state.run=null;state.files=[];$('upload-form').reset();$('file-list').replaceChildren();
  await loadList();renderDetail();notice('Пакет сохранён. Теперь можно предложить факты с цитатами.');
});});
function renderDetail() {
  const batch=state.batch,run=state.run;if(!batch)return;
  show('welcome',false);show('upload-panel',false);show('detail');
  $('batch-title').textContent=batch.entries.length+' записей';$('batch-id').textContent=batch.batch_id;
  $('batch-status').replaceWith(Object.assign(badge(run?.status||batch.status,run?.stale),{id:'batch-status'}));show('stale-warning',Boolean(run?.stale));
  $('sources').replaceChildren();batch.entries.forEach(entry=>{const details=node('details');const summary=node('summary',entry.filename+' ');summary.append(badge(entry.status));details.append(summary);
    if(entry.reason)details.append(node('p',entry.reason,'error-text'));details.append(node('pre',entry.text,'source-text'));$('sources').append(details);});
  $('saved-claims').replaceChildren();if(run)run.claims.forEach((c,i)=>$('saved-claims').append(renderClaim(c,i)));
  else $('saved-claims').append(node('p','Черновик пока не создан. Добавьте факты вручную, опираясь на цитаты.','muted'));
  show('edit-draft',can('submit_draft')&&batch.status==='ingested');show('draft-form',false);
  if(!run&&can('submit_draft')&&batch.status==='ingested')startDraft();
  $('checks').replaceChildren();
  if(run)run.checks.forEach(c=>{const row=node('div',undefined,'check-row');const body=node('div');body.append(node('strong',checkNames[c.check_no-1]),node('p',c.reason));if(c.reviewer_id)body.append(node('p','Проверяющий: '+c.reviewer_id));row.append(node('span',c.check_no,'check-num'),body,badge(c.verdict));$('checks').append(row);});
  else $('checks').append(node('p','Проверки появятся после сохранения черновика.','muted'));
  $('run-status').replaceWith(Object.assign(badge(run?.status||'Нет черновика',run?.stale),{id:'run-status'}));
  const reviewable=run&&!run.stale&&run.status!=='published'&&run.created_by!==state.session.actor_id;
  show('review-form',Boolean(can('review_run')&&reviewable));$('review-form').reset();
  $('review-message').textContent=run?.created_by===state.session.actor_id?'Черновик должен проверить другой участник.':run?'Сверьте исходники и историю публикаций перед подтверждением.':'';
  show('publish-panel',Boolean(can('publish')&&run&&!run.stale&&run.status==='ready'&&run.checks.length===4&&run.checks.every(c=>c.verdict==='pass')));
}
function renderClaim(claim,index) {const card=node('article',undefined,'claim-card');card.append(node('h3',(index+1)+'. '+claim.text),node('p',claim.subject+' / '+claim.predicate+': '+claim.value),node('blockquote',claim.quote),node('p','Источник: '+(state.batch?.entries[claim.source_index]?.filename||('запись '+(claim.source_index+1))),'hint'));return card;}
function startDraft() {$('claim-editor').replaceChildren();(state.run?.claims||[null]).forEach(addClaim);show('draft-form');}
$('edit-draft').addEventListener('click',startDraft);
function addClaim(initial=null) {
  if($('claim-editor').children.length>=50){notice('В одном черновике не более 50 фактов.',true);return;}
  const card=node('div',undefined,'claim-editor');const top=node('div',undefined,'section-title');top.append(node('h3','Факт с подтверждением'));const remove=node('button','Убрать','quiet');remove.type='button';remove.addEventListener('click',()=>card.remove());top.append(remove);card.append(top);
  const fields={};
  function field(key,label,tag='input'){const id='claim-'+crypto.randomUUID();const lab=node('label',label);lab.htmlFor=id;const el=node(tag);el.id=id;el.dataset.field=key;el.required=true;if(tag==='textarea')el.rows=3;card.append(lab,el);fields[key]=el;return el;}
  const source=field('source_index','Исходная запись','select');state.batch.entries.forEach(e=>{const option=node('option',e.filename);option.value=e.index;source.append(option);});
  const quote=field('quote','Точная цитата','textarea');const match=field('match','Место цитаты','select');const preview=node('pre',undefined,'source-text quote-preview');card.append(preview);
  field('text','Что зафиксировано','textarea');const three=node('div',undefined,'three');card.append(three);
  for(const [key,label]of[['subject','О чём / о ком'],['predicate','Что фиксируем'],['value','Значение']]){const start=card.children.length;field(key,label);const wrap=node('div');while(card.children.length>start)wrap.append(card.children[start]);three.append(wrap);}
  function locate(preferred){const text=state.batch.entries[Number(source.value)]?.text||'';const q=quote.value;match.replaceChildren();
    if(q){let from=0,index;while((index=text.indexOf(q,from))!==-1){const option=node('option','Совпадение '+(match.children.length+1));option.value=Array.from(text.slice(0,index)).length;match.append(option);from=index+Math.max(1,q.length);if(match.children.length>=100)break;}}
    if(preferred!==undefined&&Array.from(match.options).some(o=>o.value===String(preferred)))match.value=String(preferred);
    if(!match.children.length){match.append(Object.assign(node('option','Цитата не найдена'),{value:''}));}
    highlight();
  }
  function highlight(){const text=Array.from(state.batch.entries[Number(source.value)]?.text||'');const start=Number(match.value);const length=Array.from(quote.value).length;preview.replaceChildren();
    if(match.value===''){preview.textContent='Вставьте точную цитату из исходника.';return;}
    preview.append(document.createTextNode(text.slice(Math.max(0,start-80),start).join('')),node('mark',text.slice(start,start+length).join('')),document.createTextNode(text.slice(start+length,start+length+120).join('')));
  }
  source.addEventListener('change',()=>locate());quote.addEventListener('input',()=>locate());match.addEventListener('change',highlight);
  if(initial){for(const [key,el]of Object.entries(fields))if(key in initial)el.value=initial[key];}
  locate(initial?.start);$('claim-editor').append(card);
}
$('add-claim').addEventListener('click',()=>addClaim());
$('draft-form').addEventListener('submit',event=>{event.preventDefault();perform(async()=>{
  const claims=Array.from($('claim-editor').children).map(card=>{const get=key=>card.querySelector('[data-field="'+key+'"]').value;
    if(get('match')==='')throw Error('Цитата не найдена в выбранном исходнике.');
    return{source_index:Number(get('source_index')),start:Number(get('match')),quote:get('quote'),text:get('text'),subject:get('subject'),predicate:get('predicate'),value:get('value')};});
  if(!claims.length)throw Error('Добавьте хотя бы один факт.');
  state.run=await invoke('submit_draft',{batch_id:state.batch.batch_id,claims,model_version:null,prompt_version:null},true);
  await loadList();renderDetail();notice('Черновик сохранён. Проверки и замечания показаны ниже.');
});});
async function review(approve) {
  if(approve&&!['accuracy','coverage','consistency'].every(id=>$(id).checked))throw Error('Для подтверждения отметьте все три пункта после проверки.');
  const rationale=$('rationale').value.trim();if(!rationale)throw Error('Укажите основание оценки или замечания.');
  state.run=await invoke('review_run',{run_id:state.run.run_id,artifact_hash:state.run.artifact_hash,accuracy_pass:approve,
    consistency_pass:approve,coverage_confirmed:approve,rationale},true);await loadList();renderDetail();notice(approve?'Оценка сохранена.':'Замечания сохранены; публикация заблокирована.');
}
$('review-form').addEventListener('submit',event=>{event.preventDefault();perform(()=>review(true));});
$('reject-review').addEventListener('click',()=>perform(()=>review(false)));
$('publish').addEventListener('click',()=>$('publish-dialog').showModal());
$('cancel-publish').addEventListener('click',()=>$('publish-dialog').close());
$('confirm-publish').addEventListener('click',()=>perform(async()=>{state.run=await invoke('publish',{run_id:state.run.run_id,artifact_hash:state.run.artifact_hash},true);$('publish-dialog').close();await loadList();renderDetail();notice('Результат опубликован.');}));
$('history').addEventListener('click',()=>perform(async()=>{const data=await invoke('get_history',{});const content=$('history-content');content.replaceChildren();
  if(data.truncated)content.append(node('p','Показаны последние 100 версий. Публикация новых результатов заблокирована до расширения проверки истории.','boundary warning'));
  if(!data.records.length)content.append(node('p','Публикаций пока нет.','muted'));
  data.records.forEach(record=>{const card=node('article',undefined,'claim-card');card.append(badge('published',record.stale),node('p',record.run_id,'code'));
    record.claims.forEach(claim=>{card.append(node('p',claim.text),node('blockquote',claim.quote),node('p',claim.subject+' / '+claim.predicate+': '+claim.value,'hint'));});content.append(card);});$('history-dialog').showModal();
}));
$('close-history').addEventListener('click',()=>$('history-dialog').close());
