/* AgentRun controls inside the deployed SPECTRA map panels. */
(function () {
    'use strict';
    const active = new Set(['queued', 'planning', 'running', 'retrying', 'cancelling']);
    const labels = {queued: '排队中', planning: '规划中', running: '执行中', retrying: '重试中',
        waiting_user: '等待确认', blocked: '调查受阻', failed: '执行失败', not_supported: '数据能力不支持',
        external_service_unavailable: '外部服务暂不可用', cancelling: '正在取消', cancelled: '已取消',
        completed: '已完成', skipped: '不适用', pending: '待执行'};
    const titles = {'run.created': '已建立调查', 'plan.updated': '已更新计划', 'step.started': '开始执行步骤',
        'step.completed': '步骤完成', 'step.skipped': '步骤不适用', 'tool.called': '工具开始执行',
        'tool.completed': '工具结果已保存', 'step.failed': '步骤未完成', 'user.required': '需要确认',
        'provider.requested': '模型调用记录', 'run.completed': '调查已通过验收', 'run.blocked': '调查受阻',
        'run.failed': '调查失败', 'run.waiting_user': '调查已暂停', 'run.retrying': '准备重试',
        'run.cancelling': '正在取消', 'run.cancelled': '调查已取消'};
    const $ = id => document.getElementById(id);
    const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
    let run = null, store = null, events = [], source = null, timer = null, busy = false, loading = false;
    let generation = 0, connection = '尚未连接', notice = '', retryRequest = null;
    const key = () => window.crypto?.randomUUID?.() || `run-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    function url(path = '') { return `/api/v2/agent/runs/${run.id}/${path}`; }
    async function api(path, body) {
        const response = await fetch(path, {method: body ? 'POST' : 'GET', cache: 'no-store',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': document.querySelector('[name=csrfmiddlewaretoken]')?.value || ''},
            ...(body ? {body: JSON.stringify(body)} : {})});
        const result = await response.json();
        if (!response.ok) throw new Error(result.msg || result.error?.message || '服务请求失败，请重试');
        return result.data;
    }
    function message(text) {
        notice = text;
        if (!$('map-run-notice')) {
            const el = document.createElement('p'); el.id = 'map-run-notice'; el.className = 'muted-note';
            el.setAttribute('role', 'status'); $('agent-console').append(el);
        }
        $('map-run-notice').textContent = text;
    }
    function stop() { source?.close(); source = null; clearInterval(timer); timer = null; }
    function ingest(items) { events.push(...store.ingest(items)); }
    function safeLink(uri) {
        try { const parsed = new URL(uri, location.origin); return parsed.origin === location.origin && parsed.pathname.startsWith(url('artifacts/')) ? parsed.href : ''; }
        catch (_) { return ''; }
    }
    function button(action, text) { return `<button type="button" class="agent-secondary-btn" data-run-action="${action}" ${busy ? 'disabled' : ''}>${text}</button>`; }
    function elapsed() {
        const start = Date.parse(run.started_at || run.created_at), end = run.completed_at ? Date.parse(run.completed_at) : Date.now();
        const seconds = Math.max(0, Math.floor((end - start) / 1000));
        return Number.isFinite(seconds) ? `${Math.floor(seconds / 60)}分${seconds % 60}秒` : '—';
    }
    function render() {
        if (!run) return;
        const panel = $('agent-session-panel'), scroll = panel.scrollTop;
        const opened = new Set([...panel.querySelectorAll('details[open][id]')].map(el => el.id));
        const focus = panel.contains(document.activeElement) ? document.activeElement?.dataset.runAction : null;
        const steps = run.steps || [], current = steps.find(item => item.id === run.current_step_id);
        const accepted = run.status === 'completed' && run.acceptance?.passed === true && steps.length > 0
            && steps.every(step => ['completed', 'skipped'].includes(step.status) && !step.error);
        const status = run.status === 'completed' && !accepted ? '证据验收未通过' : labels[run.status] || '状态待核对';
        const evidence = (run.evidence || []).filter(item => item.id.startsWith(`v${run.plan_version}:`));
        const artifacts = (run.artifacts || []).filter(item => item.id.startsWith(`file:${run.plan_version}:`));
        panel.hidden = false; $('agent-empty').hidden = true;
        $('agent-run-btn').disabled = busy || active.has(run.status);
        $('agent-run-btn').innerHTML = '<i class="ri-play-line" aria-hidden="true"></i>';
        let actions = active.has(run.status) ? (run.status === 'cancelling' ? '' : button('pause', '暂停并修改') + button('cancel', '取消任务')) : '';
        if (['waiting_user', 'blocked', 'failed', 'external_service_unavailable', 'not_supported'].includes(run.status)) {
            actions = (run.status === 'not_supported' ? '' : button('retry_step', run.status === 'waiting_user' ? '继续 / 重试当前步骤' : '重试当前步骤')) + button('replan', '修改目标并重新规划') + button('cancel', '取消任务');
        }
        if (['completed', 'cancelled'].includes(run.status)) actions = button('rerun', '重新运行');
        panel.innerHTML = `<div class="agent-session-head"><span>${escape(status)}</span><b>#${run.id}</b><div class="agent-goal-echo">目标：${escape(run.goal)}</div></div>
            <div class="agent-observer"><div class="agent-observer-head"><span>当前步骤</span><b>${escape(current?.label || '等待执行')}</b></div>
            <p>${escape(elapsed())} · ${escape(run.provider)} / ${escape(run.model)}</p><p role="status">${escape(connection)}</p></div>
            <details id="map-run-plan" class="agent-event-log"><summary>执行计划 · ${steps.filter(s => s.status === 'completed').length} / ${steps.length}</summary><ol class="agent-steps">${steps.map(s => `<li class="agent-step"><b>${escape(s.label)}</b><small>${escape(labels[s.status] || '待执行')}${s.error ? ' · ' + escape(s.error) : ''}</small></li>`).join('')}</ol></details>
            ${run.error || run.waiting ? `<div class="agent-waiting">${escape(run.error || run.waiting.message)}<p>继续后从「${escape(current?.label || run.current_step_id)}」恢复。</p></div>` : ''}
            <div class="agent-options">${actions}</div>
            <details id="map-run-events" class="agent-event-log"><summary>执行记录（${events.length}）</summary><ol>${events.map(e => `<li class="agent-event-item"><b>#${e.sequence} ${escape(titles[e.type] || '运行记录')}</b><p>${escape(e.payload?.reason || e.payload?.message || steps.find(s => s.id === e.step_id)?.label || e.payload?.name || '')}</p></li>`).join('')}</ol></details>
            ${accepted && run.final ? `<div class="agent-final-answer"><b>调查结论</b><p>${escape(run.final.content)}</p><div class="agent-options">${(run.final.evidence_refs || []).map(ref => `<button type="button" data-evidence-ref="${escape(ref)}">查看证据 ${escape(ref)}</button>`).join('')}</div><b>限制</b><ul>${(run.final.limitations || []).map(text => `<li>${escape(text)}</li>`).join('')}</ul></div>` : ''}
            <details id="map-run-evidence" class="agent-event-log"><summary>证据与产物（${evidence.length} 项证据）</summary>
            ${evidence.map(item => `<details data-evidence-id="${escape(item.id)}"><summary>${escape(({computed_metric: '计算结果', data_fact: '数据事实', model_inference: '模型推断', temporal_change: '变化结果'})[item.kind] || '证据')} · ${escape(item.metric || item.scene_id)}</summary><p>引用：${escape(item.id)}</p><p>方法：${escape(item.method)}</p><p>${escape(typeof item.value === 'object' ? JSON.stringify(item.value) : item.value)}</p><p>${escape((item.limitations || []).join('；'))}</p></details>`).join('')}
            ${artifacts.filter(item => safeLink(item.uri)).map(item => `<p><a class="agent-report-link" href="${escape(safeLink(item.uri))}" target="_blank" rel="noopener">${escape(item.title)}</a></p>`).join('')}</details>`;
        for (const id of opened) if ($(id)) $(id).open = true;
        panel.scrollTop = scroll;
        if (focus) panel.querySelector(`[data-run-action="${focus}"]`)?.focus({preventScroll: true});
        message(notice);
    }
    async function refresh() {
        if (!run || loading) return;
        loading = true;
        const expected = generation, path = url();
        try {
            const detail = await api(path);
            if (expected !== generation) return;
            let page;
            do {
                page = await api(`${path}events/?after=${store.cursor}&limit=200`);
                if (expected !== generation) return;
                const before = store.cursor; ingest(page.events || []);
                if (page.has_more && store.cursor === before) throw new Error('事件序列不连续，正在重新读取');
            } while (page.has_more);
            run = detail;
            if (!active.has(run.status)) { stop(); connection = '已同步'; }
            render();
        } catch (error) {
            if (expected === generation) { connection = '离线查看 · 正在重连'; message(error.message); render(); }
        } finally { if (expected === generation) loading = false; }
    }
    function connect() {
        stop();
        timer = setInterval(refresh, 3000);
        if (!window.EventSource) { connection = '正在同步'; return; }
        const expected = generation;
        source = new EventSource(`${url('events/stream/')}?after=${store.cursor}`);
        source.onopen = () => { if (expected === generation) { connection = '已连接'; render(); } };
        source.addEventListener('agent_event', event => {
            if (expected !== generation) return;
            try { ingest([JSON.parse(event.data)]); refresh(); } catch (_) { refresh(); }
        });
        source.onerror = () => { if (expected === generation) { connection = '正在重连'; render(); } };
    }
    async function open(item) {
        generation += 1; loading = false; stop(); run = item; events = []; store = new window.RunEventStore(item.id);
        const link = new URL(location.href); link.searchParams.set('run', item.id); history.replaceState(null, '', link);
        connection = '正在恢复'; connect(); await refresh();
    }
    async function start(extra = {}) {
        if (busy || (run && active.has(run.status))) return;
        const goal = (extra.goal || $('agent-goal-input').value || '').trim();
        if (!goal) { message('请输入调查目标'); $('agent-goal-input').focus(); return; }
        busy = true; $('agent-run-btn').disabled = true;
        const body = {...extra, goal, mode: $('agent-mode-select').value};
        const fingerprint = JSON.stringify(body);
        if (retryRequest?.fingerprint !== fingerprint) retryRequest = {fingerprint, request_id: key()};
        try { const item = await api('/api/v2/agent/runs/', {...body, request_id: retryRequest.request_id}); retryRequest = null; notice = ''; await open(item); }
        catch (error) { message(error.message); }
        finally { busy = false; if (run) render(); else $('agent-run-btn').disabled = false; }
    }
    async function act(action) {
        if (busy || !run) return;
        if (action === 'rerun') return start({goal: run.goal});
        if (action === 'replan') {
            // Edit the target in the existing composer; no second workspace or modal theme.
            $('agent-goal-input').value = run.goal;
            $('agent-goal-input').focus(); $('agent-goal-input').select();
            message('修改上方目标后，点击“保存新目标”重新规划。已保存的执行记录会保留。');
            if (!$('map-run-save-goal')) {
                const btn = document.createElement('button'); btn.id = 'map-run-save-goal'; btn.type = 'button';
                btn.className = 'agent-secondary-btn'; btn.textContent = '保存新目标'; btn.dataset.runAction = 'save-goal';
                $('agent-session-panel').querySelector('.agent-options').append(btn);
            }
            return;
        }
        busy = true; generation += 1; loading = false; stop();
        try {
            if (action === 'save-goal') {
                const goal = $('agent-goal-input').value.trim();
                if (!goal) throw new Error('调查目标不能为空');
                await api(url('replan/'), {goal, request_id: key()});
            } else await api(url('actions/'), {action, message_id: key()});
            notice = ''; connect(); await refresh();
        } catch (error) { message(error.message); if (active.has(run.status)) connect(); }
        finally { busy = false; render(); }
    }
    window.SatelliteRun = {start};
    document.addEventListener('DOMContentLoaded', () => {
        if (document.body.dataset.runEngine !== 'dag') return;
        $('agent-session-panel').addEventListener('click', event => {
            const action = event.target.closest('[data-run-action]'); if (action) act(action.dataset.runAction);
            const ref = event.target.closest('[data-evidence-ref]');
            if (ref) {
                $('map-run-evidence').open = true;
                const target = [...document.querySelectorAll('[data-evidence-id]')].find(el => el.dataset.evidenceId === ref.dataset.evidenceRef);
                if (target) { target.open = true; target.querySelector('summary').focus(); target.scrollIntoView({block: 'nearest'}); }
            }
        });
        const id = new URL(location.href).searchParams.get('run');
        if (id && /^[1-9]\d*$/.test(id)) open({id: Number(id)});
    });
    window.addEventListener('pagehide', stop);
})();
