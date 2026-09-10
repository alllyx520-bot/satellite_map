/* Browser interaction fixtures; does not call a model or create server-side runs. */
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
    const browser = await chromium.launch({headless: true, channel: 'msedge'});
    try {
        const context = await browser.newContext({viewport: {width: 1440, height: 1000}, reducedMotion: 'reduce'});
        const page = await context.newPage(), errors = [], writes = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.addInitScript(() => {
            window.EventSource = class {
                constructor() { this.listeners = {}; window.fixtureStream = this; }
                addEventListener(type, callback) { this.listeners[type] = callback; }
                close() {}
            };
        });
        let run = {id: 987654, goal: '隔离交互验收', status: 'running', plan_version: 1,
            current_step_id: 'quality_gate', provider: 'glm', model: 'fixture', created_at: new Date().toISOString(),
            steps: [{id: 'quality_gate', label: '影像质量检查', status: 'running'}], evidence: [], artifacts: []};
        const event = sequence => ({schema_version: 1, run_id: run.id, sequence, type: 'step.started', payload: {message: `事件 ${sequence}`}});
        await context.route('**/api/ai/history/**', route => route.fulfill({json: {code: 200, data: []}}));
        await context.route('**/api/v2/agent/runs/**', async route => {
            const request = route.request(), path = new URL(request.url()).pathname;
            let data = run, status = 200;
            if (request.method() === 'POST') {
                const body = request.postDataJSON(); writes.push({path, body});
                if (path.endsWith('/actions/')) run.status = {pause: 'waiting_user', retry_step: 'running', cancel: 'cancelled'}[body.action];
                else if (path.endsWith('/replan/')) { run.goal = body.goal; run.plan_version += 1; run.status = 'planning'; }
                else status = 202;
            } else if (path.endsWith('/events/')) {
                const after = Number(new URL(request.url()).searchParams.get('after'));
                data = {events: [1, 2, 3].filter(s => s > after).map(event), has_more: false};
            }
            await route.fulfill({status, json: {code: status, data}});
        });
        const base = process.env.WORKBENCH_URL || 'http://127.0.0.1:8096/agent/';
        await page.goto(base, {waitUntil: 'domcontentloaded'});
        await page.locator('#agent-goal-input').fill('隔离交互验收');
        await page.locator('#agent-run-btn').click();
        await page.getByRole('button', {name: '暂停并修改', exact: true}).waitFor();
        assert.equal(writes.length, 1);
        assert.equal(writes[0].path, '/api/v2/agent/runs/');
        await page.evaluate(() => {
            window.fixtureStream.listeners.agent_event({data: JSON.stringify({schema_version: 1, run_id: 987654, sequence: 3, type: 'step.started', payload: {}})});
        });
        await page.waitForTimeout(100);
        assert.equal(await page.locator('#map-run-events summary').textContent(), '执行记录（3）');
        await page.getByRole('button', {name: '暂停并修改', exact: true}).click();
        await page.getByRole('button', {name: '继续 / 重试当前步骤', exact: true}).waitFor();
        await page.getByRole('button', {name: '修改目标并重新规划', exact: true}).click();
        await page.locator('#agent-goal-input').fill('修改后的隔离目标');
        await page.getByRole('button', {name: '保存新目标', exact: true}).click();
        await page.getByRole('button', {name: '暂停并修改', exact: true}).waitFor();
        assert.equal(writes.at(-1).body.goal, '修改后的隔离目标');
        await page.reload({waitUntil: 'domcontentloaded'});
        await page.getByRole('button', {name: '暂停并修改', exact: true}).waitFor();
        assert.equal(writes.filter(w => w.path === '/api/v2/agent/runs/').length, 1);
        await page.getByRole('button', {name: '暂停并修改', exact: true}).click();
        await page.getByRole('button', {name: '继续 / 重试当前步骤', exact: true}).click();
        await page.getByRole('button', {name: '取消任务', exact: true}).click();
        await page.getByRole('button', {name: '重新运行', exact: true}).waitFor();
        assert.equal(run.status, 'cancelled');
        run = {...run, status: 'completed', acceptance: {passed: false}, final: {content: '禁止展示的未经验证结论', evidence_refs: [], limitations: []}};
        await page.reload({waitUntil: 'domcontentloaded'});
        await page.getByText('证据验收未通过', {exact: true}).waitFor();
        assert.equal(await page.getByText('禁止展示的未经验证结论', {exact: true}).count(), 0);
        const ref = 'v2:metric:fixture:ndwi';
        run = {...run, steps: run.steps.map(step => ({...step, status: 'completed'})), acceptance: {passed: true}, final: {content: '隔离数据复核结论', evidence_refs: [ref], limitations: ['仅用于界面测试']},
            evidence: [{id: ref, kind: 'computed_metric', metric: 'ndwi', scene_id: 'fixture', method: 'fixture', value: {mean: 0.5}, limitations: ['测试数据']}],
            artifacts: [{id: 'file:2:fixture', title: '测试报告', uri: 'javascript:alert(1)'}]};
        await page.reload({waitUntil: 'domcontentloaded'});
        await page.getByText('隔离数据复核结论', {exact: true}).waitFor();
        await page.getByRole('button', {name: `查看证据 ${ref}`, exact: true}).click();
        assert.equal(await page.locator('[data-evidence-id]').evaluate(el => el.open), true);
        assert.equal(await page.getByRole('link', {name: '测试报告'}).count(), 0);
        await page.keyboard.press('Control+k');
        assert.equal(await page.locator('#search-input').evaluate(el => el === document.activeElement), true);
        assert.equal(await page.locator('#map').count(), 1);
        assert.equal(await page.locator('#sidebar').count(), 1);
        assert.equal(await page.locator('link[href*="agent-workbench"]').count(), 0);
        assert.equal(await page.evaluate(() => document.body.scrollWidth), 1440);
        await page.screenshot({path: '.codex-runtime/map-run-integrated-fixture-20260909.png'});
        assert.deepEqual(errors, []);
        console.log('PASS: map layout, v2 creation, event deduplication, pause, retry, replan, cancel, refresh, evidence gate and links, keyboard focus');
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
