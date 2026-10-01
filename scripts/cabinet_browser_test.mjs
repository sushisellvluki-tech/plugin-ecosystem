// Real Chromium + real ASGI/PostgreSQL. The only mocked request is a UI retry probe.
import {chromium} from 'playwright';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {mkdir,readFile} from 'node:fs/promises';
const fixture=spawn(process.env.PYTHON||'python',['-m','scripts.cabinet_fixture'],{stdio:['pipe','pipe','inherit']});
const config=await new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>reject(Error('Fixture startup timed out')),20000);
  const lines=createInterface({input:fixture.stdout});
  lines.once('line',line=>{clearTimeout(timer);try{resolve(JSON.parse(line));}catch{reject(Error('Invalid fixture response'));}});
  fixture.once('exit',code=>{clearTimeout(timer);reject(Error('Fixture exited: '+code));});
});
let browser;
try {
  browser=await chromium.launch({headless:true});
  const context=await browser.newContext({viewport:{width:1280,height:900}});
  const page=await context.newPage();page.setDefaultTimeout(15000);
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  const response=await page.goto(config.url+'/cabinet');
  assert.equal(response.status(),200);assert.match(response.headers()['content-security-policy'],/frame-ancestors 'none'/);
  await mkdir('reports/browser',{recursive:true});
  await page.screenshot({path:'reports/browser/login.jpg',type:'jpeg',quality:55});
  async function login(actor){await page.locator('#token').fill(config.tokens[actor]);await page.getByRole('button',{name:'Войти',exact:true}).click();await page.locator('#workspace').waitFor({state:'visible'});await page.waitForFunction(()=>document.querySelector('#refresh').disabled===false);assert.equal(await page.locator('#token').inputValue(),'');}
  async function logout(){await page.locator('#logout').click();await page.locator('#login').waitFor({state:'visible'});assert.equal(await page.locator('#sources').textContent(),'');}
  async function notice(text){await page.waitForFunction(text=>document.getElementById('notice').textContent.includes(text),text);await page.waitForFunction(()=>!document.querySelector('#refresh').disabled);}
  await login('author');
  await page.locator('#new-batch').click();await page.locator('#account').fill('Синтетическая компания');
  const quote='Иван сдаст отчёт в пятницу.';
  const text='😀 Пролог.\n'+quote+'\n<img src=x onerror="window.__injected=true">';
  await page.locator('#files').setInputFiles({name:'Встреча.txt',mimeType:'text/plain',buffer:Buffer.from(text)});
  await page.locator('#file-id-0').waitFor();await page.waitForFunction(()=>!document.querySelector('#account').disabled);
  await page.locator('#file-id-0').fill('browser-recording');
  await page.getByRole('button',{name:'Сохранить пакет',exact:true}).click();await notice('Пакет сохранён');
  await page.locator('[data-field="quote"]').fill(quote);
  await page.locator('[data-field="text"]').fill(quote);
  await page.locator('[data-field="subject"]').fill('Отчёт Иван');
  await page.locator('[data-field="predicate"]').fill('срок');
  await page.locator('[data-field="value"]').fill('пятница');
  const keys=[];
  await page.route('**/meetings/v1/tools/invoke',async route=>{
    const body=route.request().postDataJSON();
    if(body.name==='meetings__submit_draft'){
      keys.push(route.request().headers()['idempotency-key']);
      if(keys.length===1){await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({ok:false,error:{code:'DEPENDENCY_UNAVAILABLE',retryable:true}})});return;}
    }
    await route.continue();
  });
  await page.getByRole('button',{name:'Сохранить черновик',exact:true}).click();await notice('Сервис временно занят');
  await page.getByRole('button',{name:'Сохранить черновик',exact:true}).click();await notice('Черновик сохранён');
  assert.equal(keys.length,2);assert.equal(keys[0],keys[1]);assert.ok(keys[0]);
  await page.unroute('**/meetings/v1/tools/invoke');
  assert.equal(await page.locator('#review-form').isVisible(),false);assert.equal(await page.locator('#publish-panel').isVisible(),false);
  assert.equal(await page.locator('#checks .check-row').count(),4);
  assert.equal(await page.evaluate(()=>window.__injected),undefined);assert.equal(await page.locator('img').count(),0);
  assert.deepEqual(await page.evaluate(()=>[localStorage.length,sessionStorage.length]),[0,0]);assert.equal((await context.cookies()).length,0);
  await logout();assert.ok(!(await page.locator('body').textContent()).includes(quote));
  await login('reviewer');await page.locator('.batch-row').first().click();await page.locator('#review-form').waitFor({state:'visible'});await page.waitForFunction(()=>!document.querySelector('#accuracy').disabled);
  await page.locator('#history').click();await page.locator('#history-dialog').waitFor({state:'visible'});await page.locator('#close-history').click();
  await page.locator('#accuracy').check();await page.locator('#coverage').check();await page.locator('#consistency').check();
  await page.locator('#rationale').fill('Синтетический исходник и история сверены в браузерном тесте.');
  await page.getByRole('button',{name:'Подтвердить проверки',exact:true}).click();await notice('Оценка сохранена');
  assert.equal(await page.locator('#checks .badge.good').count(),4);
  await logout();await login('author');await page.locator('.batch-row').first().click();await page.locator('#publish-panel').waitFor({state:'visible'});await page.waitForFunction(()=>!document.querySelector('#publish').disabled);
  await page.locator('#publish').click();await page.locator('#confirm-publish').click();await notice('Результат опубликован');
  assert.equal(await page.locator('#run-status').textContent(),'Опубликован');
  await page.evaluate(()=>window.scrollTo(0,0));
  await page.screenshot({path:'reports/browser/desktop.jpg',type:'jpeg',quality:55});
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),'Mobile layout overflows');
  await page.evaluate(()=>window.scrollTo(0,0));
  await page.screenshot({path:'reports/browser/mobile.jpg',type:'jpeg',quality:55});
  await logout();await login('outsider');await page.locator('#empty-list').waitFor({state:'visible'});assert.equal(await page.locator('.batch-row').count(),0);
  assert.ok(!(await page.locator('body').textContent()).includes(quote));
  assert.deepEqual(errors,[]);
  console.log('BROWSER_RESULT '+JSON.stringify({status:'passed',checks:['real_txt_upload','unicode_quote_offsets','independent_review','publication','same_key_ui_retry','tenant_switch_clears_data','no_persistent_token','xss_literal_text','mobile_layout','no_page_errors']}));
  // Synthetic screenshots only; let the operator inspect them without exposing tokens.
  for(const name of ['login','desktop','mobile'])console.log('BROWSER_SCREENSHOT '+name+' '+(await readFile('reports/browser/'+name+'.jpg')).toString('base64'));
} finally {if(browser)await browser.close();fixture.stdin.end();await new Promise(resolve=>{if(fixture.exitCode!==null)return resolve();fixture.once('exit',resolve);setTimeout(()=>{fixture.kill();resolve();},10000).unref();});}
