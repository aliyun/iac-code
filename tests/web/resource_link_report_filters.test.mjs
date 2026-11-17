// Offline report UI verification. Uses a fresh headless browser, never the cloud-test Chrome/profile.
// RESOURCE_LINK_PLAYWRIGHT_ROOT=/path/to/node_modules node --test tests/web/resource_link_report_filters.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';

const root=process.env.RESOURCE_LINK_PLAYWRIGHT_ROOT;
const enabled=Boolean(root);
const html=()=>execFileSync('uv',['run','python','-c',`
from scripts.web.e2e.resource_link_workflow.reports import document
def row(dimension,iframe,opened,creation='untested'):
    return {'dimension':dimension,'source_type':dimension+' fixture','status':{'iframe':iframe,'open':opened},
            'creation':{'status':creation,'label':creation},'reason':'offline fixture'}
groups=[
    {'service':'VPC','name':'VSwitch','key':'vswitch','rows':[
        row('ROS','pass','fail','pass'),row('TF','product_gap','untested','fail'),
        row('QUERY','pass','pass'),row('ROS','unsupported','pass','pass')]},
    {'service':'VPC','name':'VPC','key':'vpc','rows':[row('CREATE','untested','untested')]},
    {'service':'ECS','name':'Instance','key':'ecs','rows':[row('ROS','fail','pass','pass')]},
]
print(document('Offline report',groups))
`],{encoding:'utf-8'});

test('three report filters update every product, heading, metrics, columns and grouped rows',
  {skip:!enabled},async()=>{
    const {chromium}=createRequire(import.meta.url)(`${root}/playwright`);
    const browser=await chromium.launch({headless:true,
      ...(process.env.RESOURCE_LINK_REPORT_BROWSER?{executablePath:process.env.RESOURCE_LINK_REPORT_BROWSER}:{})});
    try{
      const page=await browser.newPage({viewport:{width:1365,height:900}});
      const errors=[];page.on('pageerror',error=>errors.push(error.message));
      await page.setContent(html());
      const press=async(key,value)=>page.locator(`.filter-button[data-filter="${key}"][data-value="${value}"]`).click();
      const vpc=page.locator('#product-1');
      const switches=vpc.locator('tbody[data-search="vpc vswitch vswitch"]');
      const count=async(status)=>Number(await vpc.locator(`.metric[data-status="${status}"] strong`).textContent());
      const created=async(status)=>Number(await vpc.locator(`.metric[data-status="${status}"] .creation-metric b`).textContent());
      const heading=()=>vpc.locator('.product-heading span').textContent();
      const totals=()=>page.locator('.filter-totals [data-total]').evaluateAll(elements=>Object.fromEntries(
        elements.map(e=>[e.dataset.total,Number(e.querySelector('strong').textContent)])));
      assert.deepEqual(await totals(),{services:2,resources:3,sources:6,clicks:12,creations:4});
      await page.locator('#tab-1').click();
      assert.deepEqual(await totals(),{services:2,resources:3,sources:6,clicks:12,creations:4});
      assert.equal(await page.locator('.filter-button[aria-pressed=true]').count(),3);
      assert.equal(await page.locator('#tab-0 .tab-count').getAttribute('data-nonzero'),'true');
      assert.equal(await page.locator('#tab-1 .tab-count').getAttribute('data-nonzero'),'true');
      assert.equal(await heading(),'2 种资源 · 5 个来源子项 · 10 项点击验收 · 3 项创建验收');
      assert.equal(await count('pass'),4);assert.equal(await count('fail'),2);assert.equal(await count('untested'),3);
      assert.equal(await created('pass'),1);assert.equal(await created('fail'),1);assert.equal(await created('untested'),1);

      await press('status','pass');
      assert.deepEqual(await totals(),{services:2,resources:2,sources:4,clicks:5,creations:2});
      assert.equal(await heading(),'1 种资源 · 3 个来源子项 · 4 项点击验收 · 1 项创建验收');
      assert.equal(await count('fail'),0);
      assert.equal(await page.locator('#tab-1 .tab-count').textContent(),'1 种');
      assert.equal(await page.locator('#tab-0 .tab-count').textContent(),'1 种');
      assert.equal(await vpc.locator('tbody:not([hidden]) .resource').getAttribute('rowspan'),'3');
      assert.equal(await vpc.locator('td.filtered-out:visible').count(),2);

      await press('source','QUERY');await press('mode','iframe');
      assert.deepEqual(await totals(),{services:1,resources:1,sources:1,clicks:1,creations:0});
      assert.equal(await page.locator('[data-total="creations"]:visible').count(),0);
      assert.equal(await page.locator('[data-total="clicks"] > span').textContent(),'项 iframe 验收');
      assert.equal(await heading(),'1 种资源 · 1 个来源子项 · 1 项 iframe 验收');
      assert.equal(await page.locator('#tab-0 .tab-count').textContent(),'0 种');
      assert.equal(await page.locator('#tab-0 .tab-count').getAttribute('data-nonzero'),'false');
      assert.equal(await page.locator('#tab-1 .tab-count').getAttribute('data-nonzero'),'true');
      for(const colorScheme of ['light','dark']){
        await page.emulateMedia({colorScheme});
        const badges=await page.locator('.tab-count').evaluateAll(elements=>elements.map(element=>{
          const style=getComputedStyle(element);
          return {nonzero:element.dataset.nonzero,weight:Number(style.fontWeight),opacity:style.opacity,
            background:style.backgroundColor};
        }));
        const active=badges.find(b=>b.nonzero==='true'),empty=badges.find(b=>b.nonzero==='false');
        assert(active.weight>=700);assert.equal(active.opacity,'1');
        assert(Number(empty.opacity)<1);assert.notEqual(active.background,empty.background);
      }
      assert.equal(await vpc.locator('tbody:not([hidden]) tr:not([hidden]) .resource').textContent(),'VSwitch');
      assert.equal(await vpc.locator('tbody:not([hidden]) .resource').getAttribute('rowspan'),'1');
      assert.equal(await vpc.locator('th[data-mode="open"]:visible').count(),0);
      assert.equal(await vpc.locator('td[data-mode="creation"]:visible').count(),0);

      await press('mode','creation');await press('status','all');
      assert.deepEqual(await totals(),{services:0,resources:0,sources:0,clicks:0,creations:0});
      assert.equal(await page.locator('[data-total="clicks"]:visible').count(),0);
      assert.equal(await page.locator('[data-total="creations"]:visible').count(),1);
      assert.equal(await heading(),'0 种资源 · 0 个来源子项 · 0 项创建验收');
      assert.equal(await count('untested'),0); // Inapplicable QUERY creation is never counted.
      assert.equal(await vpc.locator('.empty-message:visible').count(),1);

      await press('source','all');
      assert.deepEqual(await totals(),{services:2,resources:3,sources:5,clicks:0,creations:4});
      assert.equal(await heading(),'2 种资源 · 4 个来源子项 · 3 项创建验收');
      assert.equal(await count('pass'),1);assert.equal(await count('fail'),1);assert.equal(await count('untested'),1);
      assert.match(await vpc.locator('.metric[data-status="pass"] > span').first().textContent(),/创建/);
      assert.equal(await vpc.locator('td[data-mode="iframe"]:visible').count(),0);
      assert.equal(await switches.locator('.resource').getAttribute('rowspan'),'3');

      await press('source','TF');await press('mode','all');await press('status','fail');
      assert.deepEqual(await totals(),{services:1,resources:1,sources:1,clicks:1,creations:1});
      assert.equal(await heading(),'1 种资源 · 1 个来源子项 · 1 项点击验收 · 1 项创建验收');
      assert.equal(await count('fail'),1);assert.equal(await created('fail'),1);
      assert.equal(await count('pass'),0); // Creation FAIL and product_gap both counted as failures, not clicks.

      await press('source','all');await press('status','all');
      assert.deepEqual(await totals(),{services:2,resources:3,sources:6,clicks:12,creations:4});
      assert.equal(await heading(),'2 种资源 · 5 个来源子项 · 10 项点击验收 · 3 项创建验收');
      assert.equal(await switches.locator('.resource').getAttribute('rowspan'),'4');
      assert.equal(await page.locator('#tab-0 .tab-count').getAttribute('data-nonzero'),'true');
      await page.locator('#search').fill('VSwitch');
      assert.deepEqual(await totals(),{services:1,resources:1,sources:4,clicks:8,creations:2});
      assert.equal(await heading(),'1 种资源 · 4 个来源子项 · 8 项点击验收 · 2 项创建验收');
      assert.equal(await page.locator('#tab-0 .tab-count').textContent(),'0 种');
      await page.locator('#search').fill('<script>');
      assert.deepEqual(await totals(),{services:0,resources:0,sources:0,clicks:0,creations:0});
      assert.equal(await vpc.locator('.empty-message:visible').count(),1);
      assert.equal(await page.locator('.filter-summary').textContent(),'当前范围：全部 / 全部 / 全部 · 搜索：<script>');
      await page.locator('#search').fill('');
      await page.setViewportSize({width:390,height:844});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
      assert.deepEqual(errors,[]);
      await page.setContent(execFileSync('uv',['run','python','-c',
        "from scripts.web.e2e.resource_link_workflow.reports import document;print(document('Empty',[]))"],{encoding:'utf-8'}));
      assert.equal(await page.locator('.empty-message').textContent(),'暂无验收结果。');
      assert.deepEqual(errors,[]);
    }finally{await browser.close();}
  });
