document.addEventListener('DOMContentLoaded', () => {
    const MAP_MAX_ZOOM = 18;
    const SATELLITE_MAX_NATIVE_ZOOM = 16;
    const FIT_BOUNDS_MAX_ZOOM = 16;

    const normalMap = L.tileLayer('https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}', {
        subdomains: ["1", "2", "3", "4"],
        maxZoom: MAP_MAX_ZOOM,
        attribution: '&copy; 高德地图'
    });

    const satelliteMap = L.tileLayer('https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}', {
        subdomains: ["1", "2", "3", "4"],
        maxZoom: MAP_MAX_ZOOM,
        maxNativeZoom: SATELLITE_MAX_NATIVE_ZOOM,
        errorTileUrl: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=',
        attribution: '&copy; 高德地图(卫星)'
    });

    const INITIAL_VIEW = { center: [36.0, 105.0], zoom: 4 };
    const map = L.map('map', {
        minZoom: 3, maxZoom: MAP_MAX_ZOOM,
        maxBounds: [[-10, 70], [65, 140]],
        layers: [satelliteMap]
    }).setView(INITIAL_VIEW.center, INITIAL_VIEW.zoom);

    function outOfChina(lng, lat) {
        return lng < 72.004 || lng > 137.8347 || lat < 0.8293 || lat > 55.8271;
    }

    function transformLat(lng, lat) {
        let ret = -100.0 + 2.0 * lng + 3.0 * lat + 0.2 * lat * lat + 0.1 * lng * lat + 0.2 * Math.sqrt(Math.abs(lng));
        ret += (20.0 * Math.sin(6.0 * lng * Math.PI) + 20.0 * Math.sin(2.0 * lng * Math.PI)) * 2.0 / 3.0;
        ret += (20.0 * Math.sin(lat * Math.PI) + 40.0 * Math.sin(lat / 3.0 * Math.PI)) * 2.0 / 3.0;
        ret += (160.0 * Math.sin(lat / 12.0 * Math.PI) + 320 * Math.sin(lat * Math.PI / 30.0)) * 2.0 / 3.0;
        return ret;
    }

    function transformLng(lng, lat) {
        let ret = 300.0 + lng + 2.0 * lat + 0.1 * lng * lng + 0.1 * lng * lat + 0.1 * Math.sqrt(Math.abs(lng));
        ret += (20.0 * Math.sin(6.0 * lng * Math.PI) + 20.0 * Math.sin(2.0 * lng * Math.PI)) * 2.0 / 3.0;
        ret += (20.0 * Math.sin(lng * Math.PI) + 40.0 * Math.sin(lng / 3.0 * Math.PI)) * 2.0 / 3.0;
        ret += (150.0 * Math.sin(lng / 12.0 * Math.PI) + 300.0 * Math.sin(lng / 30.0 * Math.PI)) * 2.0 / 3.0;
        return ret;
    }

    function wgs84ToGcj02(lng, lat) {
        if (outOfChina(lng, lat)) return { lng, lat };
        const a = 6378245.0;
        const ee = 0.00669342162296594323;
        let dLat = transformLat(lng - 105.0, lat - 35.0);
        let dLng = transformLng(lng - 105.0, lat - 35.0);
        const radLat = lat / 180.0 * Math.PI;
        let magic = Math.sin(radLat);
        magic = 1 - ee * magic * magic;
        const sqrtMagic = Math.sqrt(magic);
        dLat = (dLat * 180.0) / ((a * (1 - ee)) / (magic * sqrtMagic) * Math.PI);
        dLng = (dLng * 180.0) / (a / sqrtMagic * Math.cos(radLat) * Math.PI);
        return { lng: lng + dLng, lat: lat + dLat };
    }

    function gcj02ToWgs84(lng, lat) {
        if (outOfChina(lng, lat)) return { lng, lat };
        const gcj = wgs84ToGcj02(lng, lat);
        return { lng: lng * 2 - gcj.lng, lat: lat * 2 - gcj.lat };
    }

    function bboxFromPoints(points) {
        return {
            min_lng: Math.min(...points.map(p => p.lng)),
            min_lat: Math.min(...points.map(p => p.lat)),
            max_lng: Math.max(...points.map(p => p.lng)),
            max_lat: Math.max(...points.map(p => p.lat))
        };
    }

    function bboxCorners(bbox) {
        return [
            { lng: bbox.min_lng, lat: bbox.min_lat },
            { lng: bbox.min_lng, lat: bbox.max_lat },
            { lng: bbox.max_lng, lat: bbox.min_lat },
            { lng: bbox.max_lng, lat: bbox.max_lat }
        ];
    }

    function mapSelectionToDataBbox(nw, se) {
        const mapBbox = bboxFromPoints([
            { lng: nw.lng, lat: nw.lat },
            { lng: se.lng, lat: se.lat }
        ]);
        const dataBbox = bboxFromPoints(bboxCorners(mapBbox).map(p => gcj02ToWgs84(p.lng, p.lat)));
        return { mapBbox, dataBbox };
    }

    function dataBboxToMapBounds(bbox) {
        const mapBbox = bboxFromPoints(bboxCorners(bbox).map(p => wgs84ToGcj02(p.lng, p.lat)));
        return [[mapBbox.min_lat, mapBbox.min_lng], [mapBbox.max_lat, mapBbox.max_lng]];
    }

    function fitMapBounds(bounds, options = {}) {
        map.fitBounds(bounds, {
            padding: [24, 24],
            maxZoom: FIT_BOUNDS_MAX_ZOOM,
            ...options
        });
    }

    let mapResizeTimer = null;
    function syncMapViewport() {
        if (!map) return;
        map.invalidateSize({ animate: false, pan: false });
    }
    requestAnimationFrame(syncMapViewport);
    setTimeout(syncMapViewport, 250);
    window.addEventListener('resize', () => {
        clearTimeout(mapResizeTimer);
        mapResizeTimer = setTimeout(syncMapViewport, 120);
    });
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) setTimeout(syncMapViewport, 80);
    });

    const LAYOUT_STORAGE_KEY = 'satelliteSenseWorkbenchLayout';
    const agentConsole = document.getElementById('agent-console');
    const workspaceSidebar = document.getElementById('sidebar');
    const agentCollapseBtn = document.getElementById('agent-collapse-btn');
    const workspaceCollapseBtn = document.getElementById('workspace-collapse-btn');
    const agentResizer = document.getElementById('agent-sidebar-resizer');
    const workspaceResizer = document.getElementById('workspace-sidebar-resizer');
    const clamp = (value, min, max) => Math.min(max, Math.max(min, value));

    function readWorkbenchLayout() {
        try {
            return JSON.parse(localStorage.getItem(LAYOUT_STORAGE_KEY) || '{}');
        } catch (e) {
            return {};
        }
    }

    function saveWorkbenchLayout(patch = {}) {
        const next = { ...readWorkbenchLayout(), ...patch };
        localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(next));
        return next;
    }

    function applyWorkbenchLayout() {
        const layout = readWorkbenchLayout();
        const agentWidth = clamp(Number(layout.agentWidth) || 360, 300, 560);
        const workspaceWidth = clamp(Number(layout.workspaceWidth) || 388, 320, 560);
        document.documentElement.style.setProperty('--agent-sidebar-width', `${agentWidth}px`);
        document.documentElement.style.setProperty('--workspace-sidebar-width', `${workspaceWidth}px`);
        document.body.classList.toggle('agent-collapsed', Boolean(layout.agentCollapsed));
        document.body.classList.toggle('workspace-collapsed', Boolean(layout.workspaceCollapsed));
        if (agentCollapseBtn) {
            const collapsed = Boolean(layout.agentCollapsed);
            agentCollapseBtn.title = collapsed ? '展开左侧 Agent' : '收起左侧 Agent';
            agentCollapseBtn.innerHTML = `<i class="${collapsed ? 'ri-arrow-right-s-line' : 'ri-arrow-left-s-line'}" aria-hidden="true"></i>`;
        }
        if (workspaceCollapseBtn) {
            const collapsed = Boolean(layout.workspaceCollapsed);
            workspaceCollapseBtn.title = collapsed ? '展开右侧工作区' : '收起右侧工作区';
            workspaceCollapseBtn.innerHTML = `<i class="${collapsed ? 'ri-arrow-left-s-line' : 'ri-arrow-right-s-line'}" aria-hidden="true"></i>`;
        }
        setTimeout(syncMapViewport, 220);
    }

    function startSidebarResize(kind, event) {
        event.preventDefault();
        const isAgent = kind === 'agent';
        const maxWidth = Math.min(560, Math.max(320, window.innerWidth - 560));
        document.body.classList.add('is-resizing-sidebar');
        const onMove = (moveEvent) => {
            const width = isAgent
                ? clamp(moveEvent.clientX, 300, maxWidth)
                : clamp(window.innerWidth - moveEvent.clientX, 320, maxWidth);
            document.documentElement.style.setProperty(
                isAgent ? '--agent-sidebar-width' : '--workspace-sidebar-width',
                `${width}px`
            );
            saveWorkbenchLayout(isAgent ? { agentWidth: width, agentCollapsed: false } : { workspaceWidth: width, workspaceCollapsed: false });
            document.body.classList.toggle(isAgent ? 'agent-collapsed' : 'workspace-collapsed', false);
            syncMapViewport();
        };
        const onUp = () => {
            document.body.classList.remove('is-resizing-sidebar');
            window.removeEventListener('mousemove', onMove);
            window.removeEventListener('mouseup', onUp);
            syncMapViewport();
        };
        window.addEventListener('mousemove', onMove);
        window.addEventListener('mouseup', onUp);
    }

    applyWorkbenchLayout();
    agentCollapseBtn?.addEventListener('click', () => {
        const collapsed = !document.body.classList.contains('agent-collapsed');
        saveWorkbenchLayout({ agentCollapsed: collapsed });
        applyWorkbenchLayout();
    });
    workspaceCollapseBtn?.addEventListener('click', () => {
        const collapsed = !document.body.classList.contains('workspace-collapsed');
        saveWorkbenchLayout({ workspaceCollapsed: collapsed });
        applyWorkbenchLayout();
    });
    agentResizer?.addEventListener('mousedown', (event) => startSidebarResize('agent', event));
    workspaceResizer?.addEventListener('mousedown', (event) => startSidebarResize('workspace', event));

    function enhanceToolbarSelects() {
        const selects = Array.from(document.querySelectorAll(
            '.toolbar-control select.custom-select, .modal-select-control select.custom-select'
        ));
        const closeControl = (control) => {
            if (!control) return;
            control.classList.remove('is-open');
            const trigger = control.querySelector('.toolbar-select-trigger');
            const menu = control.querySelector('.toolbar-select-menu');
            if (trigger) trigger.setAttribute('aria-expanded', 'false');
            if (menu) menu.hidden = true;
        };
        const closeAll = (except = null) => {
            document.querySelectorAll('.toolbar-control.is-open, .modal-select-control.is-open').forEach((control) => {
                if (control !== except) closeControl(control);
            });
        };

        selects.forEach((select) => {
            if (select.dataset.enhancedSelect === '1') return;
            const control = select.closest('.toolbar-control, .modal-select-control');
            if (!control) return;

            select.dataset.enhancedSelect = '1';
            control.classList.add('is-enhanced');
            const label = control.querySelector('span');
            const labelText = control.dataset.label || label?.textContent?.trim() || select.title || '选项';
            if (label?.id) select.setAttribute('aria-labelledby', label.id);

            const shell = document.createElement('div');
            shell.className = 'toolbar-select-shell';

            const trigger = document.createElement('button');
            trigger.type = 'button';
            trigger.className = 'toolbar-select-trigger';
            trigger.setAttribute('aria-haspopup', 'listbox');
            trigger.setAttribute('aria-expanded', 'false');

            const triggerText = document.createElement('span');
            triggerText.className = 'toolbar-select-text';
            const triggerIcon = document.createElement('i');
            triggerIcon.className = 'ri-arrow-down-s-line';
            triggerIcon.setAttribute('aria-hidden', 'true');
            trigger.appendChild(triggerText);
            trigger.appendChild(triggerIcon);

            const menu = document.createElement('div');
            menu.className = 'toolbar-select-menu';
            menu.id = `${select.id || 'toolbar-select'}-menu`;
            menu.setAttribute('role', 'listbox');
            menu.hidden = true;
            trigger.setAttribute('aria-controls', menu.id);

            const optionButtons = Array.from(select.options).map((option) => {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'toolbar-select-option';
                button.dataset.value = option.value;
                button.setAttribute('role', 'option');
                button.tabIndex = -1;

                const text = document.createElement('span');
                text.textContent = option.textContent;
                const icon = document.createElement('i');
                icon.className = 'ri-check-line';
                icon.setAttribute('aria-hidden', 'true');
                button.appendChild(text);
                button.appendChild(icon);

                button.addEventListener('click', () => {
                    if (select.value !== option.value) {
                        select.value = option.value;
                        select.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    syncSelection();
                    closeControl(control);
                    trigger.focus();
                });
                menu.appendChild(button);
                return button;
            });

            function selectedOption() {
                return select.selectedOptions[0] || select.options[0];
            }

            function syncSelection() {
                const current = selectedOption();
                triggerText.textContent = current?.textContent || '';
                trigger.setAttribute(
                    'aria-label',
                    `${labelText}：${current?.textContent || ''}`
                );
                optionButtons.forEach((button) => {
                    const selected = button.dataset.value === select.value;
                    button.classList.toggle('is-selected', selected);
                    button.setAttribute('aria-selected', String(selected));
                    const icon = button.querySelector('i');
                    if (icon) icon.style.visibility = selected ? 'visible' : 'hidden';
                });
            }

            function openMenu() {
                closeAll(control);
                control.classList.add('is-open');
                trigger.setAttribute('aria-expanded', 'true');
                menu.hidden = false;
                const selectedButton = optionButtons.find((button) => button.dataset.value === select.value);
                selectedButton?.focus();
            }

            trigger.addEventListener('click', (event) => {
                event.preventDefault();
                if (control.classList.contains('is-open')) {
                    closeControl(control);
                } else {
                    openMenu();
                }
            });

            trigger.addEventListener('keydown', (event) => {
                if (event.key === 'ArrowDown' || event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    openMenu();
                }
            });

            menu.addEventListener('keydown', (event) => {
                const currentIndex = optionButtons.indexOf(document.activeElement);
                if (event.key === 'Escape') {
                    event.preventDefault();
                    closeControl(control);
                    trigger.focus();
                } else if (event.key === 'ArrowDown') {
                    event.preventDefault();
                    optionButtons[(currentIndex + 1) % optionButtons.length]?.focus();
                } else if (event.key === 'ArrowUp') {
                    event.preventDefault();
                    optionButtons[(currentIndex - 1 + optionButtons.length) % optionButtons.length]?.focus();
                } else if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    document.activeElement?.click();
                }
            });

            select.addEventListener('change', syncSelection);
            select.after(shell);
            shell.appendChild(trigger);
            shell.appendChild(menu);
            syncSelection();
        });

        document.addEventListener('click', (event) => {
            if (!event.target.closest('.toolbar-select-shell')) closeAll();
        });
        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') closeAll();
        });
    }

    enhanceToolbarSelects();

    const styleToggle = document.getElementById('map-style-toggle');
    const imagerySourceToggle = document.getElementById('imagery-source-toggle');
    if (styleToggle) {
        styleToggle.addEventListener('change', (e) => {
            if (e.target.value === 'satellite') {
                map.removeLayer(normalMap);
                satelliteMap.addTo(map);
            } else {
                map.removeLayer(satelliteMap);
                normalMap.addTo(map);
            }
        });
    }

    let provinceLayer;
    let isMouseOverChina = false;
    const CHINA_GEOJSON_URL = "https://geo.datav.aliyun.com/areas_v3/bound/100000_full.json";

    const chatMemories = {};
    let currentActiveImage = null;
    let currentSpatialCtx = "";

    const INITIAL_ZOOM = 4;

    // ==========================================
    // Toast 通知
    // ==========================================
    function showToast(msg, type = 'info') {
        const container = document.getElementById('toast-container');
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.textContent = msg;
        container.appendChild(toast);
        setTimeout(() => { if (toast.parentNode) toast.remove(); }, 3000);
    }

    function escapeHtml(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function formatSceneDate(value) {
        if (!value) return '未知';
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return '未知';
        return date.toLocaleString('zh-CN', { hour12: false });
    }

    function sceneGradeText(grade) {
        const map = {
            reference: '参考级',
            screening: '筛查级',
            decision_support: '决策辅助级',
            evidence: '证据级'
        };
        return map[grade] || grade || '未知';
    }

    function sceneBrief(scene) {
        if (!scene) return '';
        const parts = [scene.source_label || scene.source || '影像源', sceneGradeText(scene.decision_grade)];
        if (scene.source === 'sentinel2') {
            const acquired = scene.acquired_at ? formatSceneDate(scene.acquired_at).split(' ')[0] : '';
            if (acquired) parts.push(acquired);
            if (scene.cloud_percent != null) parts.push(`云量 ${scene.cloud_percent}%`);
            if (scene.selection?.suitability_score != null) parts.push(`评分 ${scene.selection.suitability_score}`);
        }
        return parts.filter(Boolean).join(' · ');
    }

    function historyLabel(history) {
        const scene = history.scene;
        if (!scene) return (history.spatial_context || history.image_file).substring(0, 36);
        const prefix = scene.source === 'sentinel2' ? '近期公开影像' : '高清底图';
        const acquired = scene.acquired_at ? formatSceneDate(scene.acquired_at).split(' ')[0] : '';
        const detail = scene.source === 'sentinel2'
            ? [acquired, scene.cloud_percent != null ? `云量${scene.cloud_percent}%` : '', scene.selection?.suitability_score != null ? `评分${scene.selection.suitability_score}` : ''].filter(Boolean).join(' / ')
            : (history.spatial_context || scene.decision_grade_label || sceneGradeText(scene.decision_grade));
        return `${prefix}${detail ? ' · ' + detail : ''}`.substring(0, 42);
    }

    function historySubtitle(history) {
        const parts = [];
        if (history.spatial_context) parts.push(history.spatial_context);
        if (history.scene) parts.push(sceneBrief(history.scene));
        if (!parts.length && history.image_file) parts.push(history.image_file);
        return parts.filter(Boolean).join(' / ').substring(0, 88);
    }

    function historySourceCode(history) {
        const source = history.scene?.source;
        if (source === 'sentinel2') return 'S2';
        if (source === 'mapbox') return 'HD';
        return 'IMG';
    }

    function getImagerySource() {
        return imagerySourceToggle?.value === 'sentinel2' ? 'sentinel2' : 'mapbox';
    }

    function imagerySourceLabel(source) {
        return source === 'sentinel2' ? '近期公开影像' : '高清底图';
    }

    function formatCompactDate(value) {
        if (!value) return '未知时间';
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return '未知时间';
        return date.toLocaleString('zh-CN', {
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            hour12: false
        });
    }

    function estimateAreaKm2(minLat, maxLat, minLng, maxLng) {
        const midLat = ((minLat + maxLat) / 2) * Math.PI / 180;
        const heightKm = Math.abs(maxLat - minLat) * 111.32;
        const widthKm = Math.abs(maxLng - minLng) * 111.32 * Math.max(Math.cos(midLat), 0.05);
        return Math.max(widthKm * heightKm, 0);
    }

    function formatAreaKm2(value) {
        if (!Number.isFinite(value)) return '未知';
        if (value >= 100) return `${Math.round(value).toLocaleString('zh-CN')} km²`;
        if (value >= 10) return `${value.toFixed(1)} km²`;
        return `${value.toFixed(2)} km²`;
    }

    function setRegionStage(itemEl, stage, label) {
        if (!itemEl) return;
        itemEl.dataset.stage = stage;
        const chip = itemEl.querySelector('.region-stage-chip');
        if (chip) chip.textContent = label;
    }

    function updateRegionMetrics(itemEl, values = {}) {
        if (!itemEl) return;
        Object.entries(values).forEach(([key, value]) => {
            const target = itemEl.querySelector(`[data-region-metric="${key}"]`);
            if (target) target.textContent = value || '未知';
        });
    }

    // ==========================================
    // 智能调查 Agent
    // ==========================================
    const agentGoalInput = document.getElementById('agent-goal-input');
    const agentRunBtn = document.getElementById('agent-run-btn');
    const agentModeSelect = document.getElementById('agent-mode-select');
    const agentPanel = document.getElementById('agent-session-panel');
    let currentAgentSessionId = null;
    let agentPollTimer = null;

    function agentStatusText(status) {
        const map = {
            running: '运行中',
            waiting_user: '等待确认',
            completed: '已完成',
            failed: '失败'
        };
        return map[status] || status || '未知';
    }

    function agentStepText(step) {
        const mark = step.status === 'done' ? '✓' : (step.status === 'running' ? '…' : '!');
        return `<li class="agent-step agent-step-${escapeHtml(step.status || 'todo')}"><span>${mark}</span><b>${escapeHtml(step.label || step.id)}</b>${step.message ? `<small>${escapeHtml(step.message)}</small>` : ''}</li>`;
    }

    function renderAgentPlan(observer, steps) {
        const planSteps = Array.isArray(observer?.plan_steps) && observer.plan_steps.length
            ? observer.plan_steps
            : steps.map(s => ({ id: s.id, label: s.label, status: s.status === 'done' ? 'done' : (s.status === 'running' ? 'running' : 'pending') }));
        if (!planSteps.length) return '';
        return `<div class="agent-plan-strip">${planSteps.map(step => `
            <span class="agent-plan-chip agent-plan-${escapeHtml(step.status || 'pending')}">${escapeHtml(step.label || step.id)}</span>
        `).join('')}</div>`;
    }

    function renderAgentObserver(observer, steps) {
        if (!observer || Object.keys(observer).length === 0) return '';
        return `
            <div class="agent-observer">
                <div class="agent-observer-head">
                    <span>当前阶段</span>
                    <b>${escapeHtml(observer.current_label || '准备中')}</b>
                </div>
                <div class="agent-observer-grid">
                    <div><span>Agent 公开思路</span><p>${escapeHtml(observer.public_thought || '正在整理任务上下文。')}</p></div>
                    <div><span>正在做</span><p>${escapeHtml(observer.doing || '')}</p></div>
                    <div><span>下一步</span><p>${escapeHtml(observer.next || '继续按计划推进')}</p></div>
                </div>
                ${renderAgentPlan(observer, steps)}
            </div>
        `;
    }

    function renderAgentSession(session) {
        if (!agentPanel || !session) return;
        agentPanel.hidden = false;
        const artifacts = session.artifacts || {};
        const observer = session.observer || artifacts.observer || {};
        const ndwi = artifacts.ndwi;
        const finalAnswer = artifacts.final_answer || '';
        const report = artifacts.report;
        const waiting = artifacts.waiting;
        const steps = Array.isArray(session.timeline) ? session.timeline : [];
        const optionsHtml = waiting?.options?.length
            ? `<div class="agent-options">${waiting.options.map(opt => `<button data-agent-option="${escapeHtml(opt)}">${escapeHtml(opt)}</button>`).join('')}</div>`
            : '';
        const imageHtml = artifacts.image_url
            ? `<img class="agent-result-img" src="${escapeHtml(artifacts.image_url)}&t=${Date.now()}" alt="Agent 影像结果">`
            : '';
        const ndwiHtml = ndwi
            ? `<div class="agent-metric"><span>NDWI</span><b>${ndwi.available ? `${ndwi.water_percent}% 可能水体` : '未计算'}</b><p>${escapeHtml(ndwi.limitations || ndwi.reason || '')}</p></div>`
            : '';
        const reportHtml = report
            ? `<a class="agent-report-link" href="${escapeHtml(report.download_url)}" target="_blank">下载 Word 报告</a>`
            : (session.status === 'completed' ? '<button id="agent-report-btn" class="agent-secondary-btn">生成报告</button>' : '');
        agentPanel.innerHTML = `
            <div class="agent-session-head">
                <span>${escapeHtml(agentStatusText(session.status))}</span>
                <b>#${session.id}</b>
            </div>
            ${renderAgentObserver(observer, steps)}
            <ol class="agent-steps">${steps.map(agentStepText).join('')}</ol>
            ${waiting ? `<div class="agent-waiting">${escapeHtml(waiting.message || '')}${optionsHtml}</div>` : ''}
            ${imageHtml}
            ${ndwiHtml}
            ${finalAnswer ? `<div class="agent-final-answer">${renderMarkdown(finalAnswer)}</div>` : ''}
            ${reportHtml}
        `;
        agentPanel.querySelectorAll('[data-agent-option]').forEach(btn => {
            btn.addEventListener('click', () => sendAgentMessage(btn.dataset.agentOption || btn.textContent));
        });
        const reportBtn = document.getElementById('agent-report-btn');
        if (reportBtn) reportBtn.addEventListener('click', () => sendAgentMessage('生成报告', 'generate_report'));
        if (artifacts.scene) {
            const b = artifacts.bbox || artifacts.scene.bbox;
            if (b?.min_lat != null) fitMapBounds(dataBboxToMapBounds(b));
        }
    }

    async function loadAgentSession(id) {
        const res = await fetch(`/api/agent/sessions/${id}/`);
        const data = await res.json();
        if (data.code === 200) {
            renderAgentSession(data.data);
            if (data.data.status === 'completed' || data.data.status === 'failed' || data.data.status === 'waiting_user') {
                if (agentPollTimer) clearInterval(agentPollTimer);
                agentPollTimer = null;
                agentRunBtn.disabled = false;
            }
            return data.data;
        }
        throw new Error(data.msg || 'Agent 状态读取失败');
    }

    function startAgentPolling(id) {
        if (agentPollTimer) clearInterval(agentPollTimer);
        agentPollTimer = setInterval(() => {
            loadAgentSession(id).catch(() => {
                if (agentPollTimer) clearInterval(agentPollTimer);
                agentPollTimer = null;
                agentRunBtn.disabled = false;
                showToast('Agent 状态读取失败', 'error');
            });
        }, 1200);
    }

    async function startAgentSession(extra = {}) {
        const goal = (extra.goal || agentGoalInput?.value || '').trim();
        if (!goal) {
            showToast('请输入调查目标', 'warning');
            return;
        }
        agentRunBtn.disabled = true;
        if (agentPanel) {
            agentPanel.hidden = false;
            agentPanel.innerHTML = `
                <div class="agent-session-head">
                    <span>建立任务</span>
                    <b>准备中</b>
                </div>
                <div class="agent-boot-state">
                    <span class="spinner"></span>
                    <div><b>正在连接调查链路</b><small>正在整理目标、影像源和执行计划。</small></div>
                </div>
            `;
        }
        try {
            const res = await fetch('/api/agent/sessions/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    goal,
                    mode: agentModeSelect?.value || 'precise',
                    ...extra
                })
            });
            const data = await res.json();
            if (data.code !== 200) {
                agentRunBtn.disabled = false;
                showToast(data.msg || 'Agent 启动失败', 'error');
                return;
            }
            currentAgentSessionId = data.data.id;
            renderAgentSession(data.data);
            startAgentPolling(currentAgentSessionId);
            showToast('Agent 调查已启动', 'success');
        } catch (e) {
            agentRunBtn.disabled = false;
            showToast('Agent 请求失败', 'error');
        }
    }

    async function sendAgentMessage(content, action = '') {
        if (!currentAgentSessionId) return;
        try {
            const res = await fetch(`/api/agent/sessions/${currentAgentSessionId}/messages/`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content, action })
            });
            const data = await res.json();
            if (data.code === 200) {
                renderAgentSession(data.data);
                if (data.data?.status === 'running') {
                    agentRunBtn.disabled = true;
                    startAgentPolling(currentAgentSessionId);
                } else if (data.data?.status === 'completed' || data.data?.status === 'failed' || data.data?.status === 'waiting_user') {
                    agentRunBtn.disabled = false;
                }
                showToast(action === 'generate_report' ? '报告已生成' : '已发送给 Agent', 'success');
            } else {
                showToast(data.msg || 'Agent 消息失败', 'error');
            }
        } catch (e) {
            showToast('Agent 消息请求失败', 'error');
        }
    }

    if (agentRunBtn) {
        agentRunBtn.addEventListener('click', () => startAgentSession());
    }
    if (agentGoalInput) {
        agentGoalInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') startAgentSession();
        });
    }

    // ==========================================
    // 返回全国按钮
    // ==========================================
    const backBtn = document.getElementById('backButton');
    const zoomReadout = document.getElementById('map-zoom-readout');
    function updateBackBtn() {
        backBtn.classList.toggle('is-visible', map.getZoom() >= 7);
        if (zoomReadout) zoomReadout.textContent = map.getZoom();
    }
    map.on('zoomend', updateBackBtn);
    map.on('moveend', updateBackBtn);
    updateBackBtn();
    backBtn.addEventListener('click', () => {
        map.setView(INITIAL_VIEW.center, INITIAL_VIEW.zoom);
        showToast('已返回全国视图', 'info');
    });

    // ==========================================
    // 空状态 & 计数
    // ==========================================
    function updateSidebarUI() {
        const items = document.querySelectorAll('.coord-item');
        const count = document.getElementById('record-count');
        const empty = document.getElementById('empty-state');
        count.textContent = items.length;
        if (empty) {
            empty.style.display = items.length === 0 ? 'flex' : 'none';
        }
    }

    // ==========================================
    // 对比模式
    // ==========================================
    let compareMode = false;
    const compareToggle = document.getElementById('compare-toggle');
    const compareBar = document.getElementById('compare-bar');
    const compareBtn = document.getElementById('compare-btn');
    if (compareBar) compareBar.hidden = true;

    compareToggle.addEventListener('click', () => {
        compareMode = !compareMode;
        compareToggle.classList.toggle('is-active', compareMode);
        document.body.classList.toggle('compare-mode', compareMode);
        if (!compareMode) {
            document.querySelectorAll('.select-cb').forEach(cb => cb.checked = false);
            compareBar.hidden = true;
        }
    });

    function updateCompareBar() {
        const checked = document.querySelectorAll('.select-cb:checked').length;
        compareBar.hidden = !(compareMode && checked >= 2);
        compareBtn.textContent = `对比所选 (${checked} 个区域)`;
    }
    document.addEventListener('change', (e) => {
        if (e.target.classList.contains('select-cb')) updateCompareBar();
    });

    compareBtn.addEventListener('click', () => {
        const checked = document.querySelectorAll('.select-cb:checked');
        const items = Array.from(checked).map(cb => {
            const item = cb.closest('.coord-item');
            const img = item.querySelector('.preview-img');
            return { fileName: img?.dataset?.filename, imgUrl: img?.src };
        }).filter(x => x.fileName);
        if (items.length < 2) return;
        openCompareModal(items);
    });

    function openCompareModal(items) {
        currentActiveImage = '__compare__';
        currentSpatialCtx = '';
        renderScenePanel(null);
        modalImg.style.display = 'none';
        modalIdSpan.innerText = ` · 对比 ${items.length} 个区域`;
        chatBox.innerHTML = `<div class="chat-empty-state">加载 ${items.length} 张影像...</div>`;

        const container = document.querySelector('.chat-modal-left');
        container.innerHTML = '';
        container.style.position = '';
        container.style.flexDirection = 'row';
        container.style.flexWrap = 'wrap';
        container.style.gap = '8px';
        container.style.alignContent = 'flex-start';
        items.forEach((item, i) => {
            const img = document.createElement('img');
            img.src = item.imgUrl;
            img.className = 'compare-preview-img';
            img.alt = `区域 ${i + 1}`;
            container.appendChild(img);
        });

        chatMemories['__compare__'] = {
            history: [],
            spatial: '',
            compareFiles: items.map(x => x.fileName)
        };
        renderChatHistory();
        modal.style.display = 'flex';
        updatePromptScene();
        setTimeout(() => textarea.focus(), 100);
    }

    // ==========================================
    // 地图提示自动隐藏
    // ==========================================
    const mapHint = document.getElementById('map-hint');
    let hintHidden = false;
    function hideHint() {
        if (!hintHidden && mapHint) {
            hintHidden = true;
            mapHint.style.opacity = '0';
            setTimeout(() => { if (mapHint) mapHint.style.display = 'none'; }, 500);
        }
    }

    async function initMapLayers() {
        try {
            const response = await fetch(CHINA_GEOJSON_URL);
            const geojson = await response.json();
            const worldOuter = [[-90, -360], [-90, 360], [90, 360], [90, -360], [-90, -360]];
            const holes = [];

            geojson.features.forEach(feature => {
                const geom = feature.geometry;
                if (geom.type === "Polygon") {
                    geom.coordinates.forEach(ring => holes.push(ring.map(c => [c[1], c[0]])));
                } else if (geom.type === "MultiPolygon") {
                    geom.coordinates.forEach(poly => poly.forEach(ring => holes.push(ring.map(c => [c[1], c[0]]))));
                }
            });

            L.polygon([worldOuter, ...holes], {
                color: 'none', fillColor: '#071113', fillOpacity: 0.82, interactive: false, renderer: L.canvas()
            }).addTo(map);

            provinceLayer = L.geoJSON(geojson, {
                style: { color: "#5bfff0", weight: 0.9, opacity: 0.45, fillOpacity: 0, fillColor: "transparent" },
                onEachFeature: (feature, layer) => {
                    layer.on({
                        mouseover: (e) => {
                            isMouseOverChina = true;
                            e.target.setStyle({ color: "#55eee5", weight: 2, fillOpacity: 0.08, fillColor: "#22d3c5" });
                            e.target.bringToFront();
                        },
                        mouseout: (e) => {
                            isMouseOverChina = false;
                            provinceLayer.resetStyle(e.target);
                        },
                        click: (e) => { if (!isSelecting && !justFinished) fitMapBounds(e.target.getBounds(), { maxZoom: 9 }); }
                    });
                }
            }).addTo(map);
        } catch (err) {
            console.error("地图加载失败", err);
            showToast('地图加载失败，请刷新重试', 'error');
        }
    }

    // ==========================================
    // 框选逻辑
    // ==========================================
    let isSelecting = false, startLatLng, selectionRect, pressTimer, justFinished = false;

    map.on('mousedown', (e) => {
        if (e.originalEvent.button !== 0 || !isMouseOverChina) return;
        pressTimer = setTimeout(() => {
            isSelecting = true;
            startLatLng = e.latlng;
            map.dragging.disable();
        }, 200);
    });

    map.on('dragstart', () => {
        if (pressTimer) {
            clearTimeout(pressTimer);
            pressTimer = null;
        }
    });

    map.on('mousemove', (e) => {
        if (!isSelecting) return;
        const bounds = L.latLngBounds(startLatLng, e.latlng);
        if (!selectionRect) {
            selectionRect = L.rectangle(bounds, { color: "#007aff", weight: 3, fillOpacity: 0.12 }).addTo(map);
        } else {
            selectionRect.setBounds(bounds);
        }
    });

    map.on('mouseup', (e) => {
        if (e.originalEvent.button !== 0) return;

        clearTimeout(pressTimer);
        pressTimer = null;

        if (isSelecting) {
            isSelecting = false;

            if (selectionRect) {
                const b = selectionRect.getBounds();
                const nw = b.getNorthWest();
                const se = b.getSouthEast();
                const item = addRecordToSidebar(nw, se);
                item._mapRect = selectionRect;
                sendToBackend(nw, se, item);
                selectionRect = null;
            }

            map.dragging.enable();

            justFinished = true;
            setTimeout(() => { justFinished = false; }, 150);
        }
    });

    map.on('contextmenu', (e) => {
        if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
        if (touchTimer) { clearTimeout(touchTimer); touchTimer = null; }
        if (selectionRect) { map.removeLayer(selectionRect); selectionRect = null; }
        if (isSelecting) {
            isSelecting = false;
            map.dragging.enable();
        }
        justFinished = false;
        e.preventDefault();
    });

    // ==========================================
    // 新增：触摸设备长按框选
    // ==========================================
    let touchStartPos, touchTimer;
    map.on('touchstart', (e) => {
        if (e.touches.length !== 1) return;
        const touch = e.touches[0];
        touchStartPos = map.mouseEventToLatLng(touch);
        touchTimer = setTimeout(() => {
            isSelecting = true;
            startLatLng = touchStartPos;
            map.dragging.disable();
            if (selectionRect) { map.removeLayer(selectionRect); selectionRect = null; }
        }, 200);
    });
    map.on('touchmove', (e) => {
        if (touchTimer) {
            clearTimeout(touchTimer);
            touchTimer = null;
        }
        if (!isSelecting) return;
        const touch = e.touches[0];
        const latlng = map.mouseEventToLatLng(touch);
        const bounds = L.latLngBounds(startLatLng, latlng);
        if (!selectionRect) {
            selectionRect = L.rectangle(bounds, { color: "#007aff", weight: 3, fillOpacity: 0.12 }).addTo(map);
        } else {
            selectionRect.setBounds(bounds);
        }
    });
    map.on('touchend', () => {
        clearTimeout(touchTimer);
        touchTimer = null;
        if (isSelecting && selectionRect) {
            isSelecting = false;
            const b = selectionRect.getBounds();
            const nw = b.getNorthWest();
            const se = b.getSouthEast();
            const item = addRecordToSidebar(nw, se);
            sendToBackend(nw, se, item);
            selectionRect = null;
            map.dragging.enable();
            justFinished = true;
            setTimeout(() => { justFinished = false; }, 150);
        }
    });

    function addRecordToSidebar(nw, se) {
        hideHint();
        const list = document.getElementById('coords-list');
        const c = list.querySelectorAll('.coord-item').length + 1;
        const { mapBbox, dataBbox } = mapSelectionToDataBbox(nw, se);
        const minLng = dataBbox.min_lng;
        const maxLng = dataBbox.max_lng;
        const minLat = dataBbox.min_lat;
        const maxLat = dataBbox.max_lat;
        const centerLat = (minLat + maxLat) / 2;
        const centerLng = (minLng + maxLng) / 2;
        const areaKm2 = estimateAreaKm2(minLat, maxLat, minLng, maxLng);
        const div = document.createElement('div');
        div.className = 'coord-item';
        div.dataset.stage = 'pending';
        div._mapBounds = [[mapBbox.min_lat, mapBbox.min_lng], [mapBbox.max_lat, mapBbox.max_lng]];
        div._dataBbox = dataBbox;
        div.innerHTML = `
            <button class="delete-btn" title="删除此项"><i class="ri-close-line" aria-hidden="true"></i></button>
            <input type="checkbox" class="select-cb" aria-label="选择区域 #${c}">
            <div class="coord-title-row">
                <strong><i class="ri-focus-3-line" aria-hidden="true"></i> 区域 #${c}</strong>
                <span>${imagerySourceLabel(getImagerySource())}</span>
            </div>
            <div class="region-stage-row">
                <span class="region-stage-chip">影像准备中</span>
                <span class="region-center">${centerLat.toFixed(4)}°N, ${centerLng.toFixed(4)}°E</span>
            </div>
            <div class="coord-meta">${maxLat.toFixed(4)}, ${minLng.toFixed(4)} → ${minLat.toFixed(4)}, ${maxLng.toFixed(4)}</div>
            <div class="region-metrics">
                <div><span>面积</span><b data-region-metric="area">${formatAreaKm2(areaKm2)}</b></div>
                <div><span>分辨率</span><b data-region-metric="gsd">待获取</b></div>
                <div><span>影像源</span><b data-region-metric="source">${imagerySourceLabel(getImagerySource())}</b></div>
            </div>
            <div class="scene-brief" hidden></div>
            <img class="preview-img" alt="卫星图预览" title="点击进入分析舱">
            <div class="tile-progress" hidden>
                <div class="progress-bar-bg">
                    <div class="progress-bar-fill"></div>
                </div>
                <span class="progress-text">0/0</span>
            </div>
            <div class="ai-status">
                <span class="spinner"></span> 正在准备影像...
            </div>
            <div class="ai-question" hidden>
                <div class="region-actions">
                    <button class="locate-region-btn secondary-action" type="button">
                        <i class="ri-crosshair-2-line" aria-hidden="true"></i>定位
                    </button>
                    <button class="enter-cabin-btn" type="button">
                        <i class="ri-door-open-line" aria-hidden="true"></i>进入分析舱
                    </button>
                </div>
            </div>
        `;

        // 删除按钮
        const delBtn = div.querySelector('.delete-btn');
        delBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            if (div._pollTimer) { clearInterval(div._pollTimer); div._pollTimer = null; }
            if (div._mapRect) map.removeLayer(div._mapRect);
            const img = div.querySelector('.preview-img');
            if (img && img.dataset.filename && chatMemories[img.dataset.filename]) {
                delete chatMemories[img.dataset.filename];
            }
            div.remove();
            updateSidebarUI();
            // 重编号
            const items = list.querySelectorAll('.coord-item');
            items.forEach((item, i) => {
                const strong = item.querySelector('strong');
                if (strong) strong.innerHTML = `<i class="ri-focus-3-line" aria-hidden="true"></i> 区域 #${i + 1}`;
                const cb = item.querySelector('.select-cb');
                if (cb) cb.setAttribute('aria-label', `选择区域 #${i + 1}`);
            });
            updateCompareBar();
            showToast('已删除该区域', 'info');
        });

        // 点击预览放大（跳转到当前区域 + 打开模态）
        const previewImg = div.querySelector('.preview-img');
        previewImg.addEventListener('click', function() {
            if (this.dataset.filename) {
                fitMapBounds(div._mapBounds || dataBboxToMapBounds(div._dataBbox));
                openChatModal(this.dataset.filename, this.src);
            }
        });
        div.querySelector('.locate-region-btn').addEventListener('click', (e) => {
            e.stopPropagation();
            fitMapBounds(div._mapBounds || dataBboxToMapBounds(div._dataBbox));
            showToast(`已定位到区域 #${c}`, 'info');
        });

        list.prepend(div);
        updateSidebarUI();
        return div;
    }

    function pollDownloadProgress(fileName, totalTiles, opts = {}) {
        const {
            ownerEl,
            progressBar,
            fill,
            progressText,
            statusEl,
            onReady,
            onError,
            successMessage = '卫星图抓取成功'
        } = opts;

        let pollCount = 0;
        let missCount = 0;
        let pollTimer = null;
        const MAX_POLLS = 240;  // 240 × 500ms ~= 2 分钟超时
        const MAX_MISS = 10;

        const stopPoll = () => {
            if (pollTimer) {
                clearInterval(pollTimer);
                pollTimer = null;
            }
            if (ownerEl) ownerEl._pollTimer = null;
        };

        const fail = (msg, toastMsg = msg) => {
            stopPoll();
            if (progressBar) progressBar.style.display = 'none';
            if (progressBar) progressBar.hidden = true;
            if (statusEl) {
                statusEl.style.display = 'block';
                statusEl.innerHTML = msg;
            }
            if (onError) onError(msg);
            showToast(toastMsg, 'error');
            return null;
        };

        return new Promise((resolve) => {
            pollTimer = setInterval(async () => {
                pollCount++;
                if (pollCount > MAX_POLLS) {
                    resolve(fail('下载超时，请重试'));
                    return;
                }
                try {
                    const pr = await fetch(`/api/satellite/progress/?file=${fileName}`);
                    const pd = await pr.json();
                    if (pd.code === 200 && pd.data) {
                        missCount = 0;
                        const done = pd.data.done || 0;
                        const pct = Math.round((done / Math.max(totalTiles, 1)) * 100);
                        if (fill) fill.style.width = pct + '%';
                        if (progressText) progressText.textContent = `${done}/${totalTiles}`;

                        if (pd.data.status === 'done' || pd.data.status === 'partial') {
                            stopPoll();
                            if (progressBar) progressBar.style.display = 'none';
                            if (progressBar) progressBar.hidden = true;
                            if (statusEl) statusEl.style.display = 'none';
                            if (onReady) onReady(pd.data);
                            if (pd.data.status === 'partial') {
                                showToast(`卫星图已加载，但 ${pd.data.failed || 0} 个瓦片失败`, 'warning');
                            } else {
                                showToast(successMessage, 'success');
                            }
                            resolve(pd.data);
                        } else if (pd.data.status === 'error') {
                            resolve(fail('抓取失败，请重试', '抓取失败'));
                        }
                    } else {
                        missCount++;
                        if (missCount >= MAX_MISS) {
                            resolve(fail('进度丢失，请重试', '下载进度丢失'));
                        }
                    }
                } catch (e) {
                    missCount++;
                    if (missCount >= MAX_MISS) {
                        resolve(fail('网络异常，请重试'));
                    }
                }
            }, 500);
            if (ownerEl) ownerEl._pollTimer = pollTimer;
        });
    }

    function loadRegionPreviewImage(img, baseUrl, fileName, retries = 3) {
        return new Promise((resolve) => {
            let attempt = 0;
            const cleanup = () => {
                img.onload = null;
                img.onerror = null;
            };
            const tryLoad = () => {
                attempt += 1;
                cleanup();
                img.onload = () => {
                    cleanup();
                    img.dataset.filename = fileName;
                    resolve(true);
                };
                img.onerror = () => {
                    if (attempt < retries) {
                        setTimeout(tryLoad, 350 * attempt);
                    } else {
                        cleanup();
                        resolve(false);
                    }
                };
                img.removeAttribute('src');
                img.src = `${baseUrl}&t=${Date.now()}_${attempt}`;
            };
            tryLoad();
        });
    }

    async function sendToBackend(nw, se, itemEl) {
        const selected = itemEl?._dataBbox || mapSelectionToDataBbox(nw, se).dataBbox;
        const min_lng = selected.min_lng;
        const max_lng = selected.max_lng;
        const min_lat = selected.min_lat;
        const max_lat = selected.max_lat;

        const status = itemEl.querySelector('.ai-status');
        const previewImg = itemEl.querySelector('.preview-img');
        const questionBox = itemEl.querySelector('.ai-question');
        const enterBtn = itemEl.querySelector('.enter-cabin-btn');
        const progressBar = itemEl.querySelector('.tile-progress');
        const fill = progressBar.querySelector('.progress-bar-fill');
        const progressText = progressBar.querySelector('.progress-text');
        const imagerySource = getImagerySource();
        const endpoint = imagerySource === 'sentinel2' ? "/api/satellite/get-sentinel-img/" : "/api/satellite/get-img/";

        try {
            setRegionStage(itemEl, 'pending', '请求影像中');
            status.style.display = 'block';
            status.innerHTML = `<span class="spinner"></span> 正在获取${imagerySourceLabel(imagerySource)}...`;

            const r = await fetch(endpoint, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ min_lng, max_lng, min_lat, max_lat })
            });
            const d = await r.json();

            if (d.code === 200) {
                const fileName = d.data.file_name;
                const totalTiles = d.data.total_tiles || 1;
                const imgUrl = `/api/satellite/show-img/?file=${fileName}`;
                const spatialCtx = d.data.gsd_m
                    ? `范围: ${d.data.area_km2} km\u00B2 | 分辨率: ${d.data.gsd_m} m/像素`
                    : "";
                updateRegionMetrics(itemEl, {
                    area: d.data.area_km2 ? formatAreaKm2(Number(d.data.area_km2)) : undefined,
                    gsd: d.data.gsd_m ? `${d.data.gsd_m} m/像素` : undefined,
                    source: imagerySourceLabel(imagerySource)
                });

                chatMemories[fileName] = {
                    history: [],
                    spatial: spatialCtx,
                    bbox: { min_lng, max_lng, min_lat, max_lat },
                    gsd: d.data.gsd_m,
                    sceneId: d.data.scene_id,
                    scene: d.data.scene
                };
                const sceneBriefEl = itemEl.querySelector('.scene-brief');
                if (sceneBriefEl && d.data.scene) {
                    sceneBriefEl.textContent = sceneBrief(d.data.scene);
                    sceneBriefEl.hidden = false;
                }

                const showReadyImage = async () => {
                    setRegionStage(itemEl, 'downloading', '预览加载中');
                    status.style.display = 'block';
                    status.innerHTML = '<span class="spinner"></span> 正在加载预览...';
                    previewImg.style.display = 'none';
                    const loaded = await loadRegionPreviewImage(previewImg, imgUrl, fileName);
                    if (!loaded) {
                        setRegionStage(itemEl, 'error', '预览失败');
                        status.style.display = 'block';
                        status.innerHTML = '预览图加载失败，请重新框选或刷新重试';
                        showToast('预览图加载失败，请重试', 'error');
                        return;
                    }
                    setRegionStage(itemEl, 'ready', '可进入分析');
                    status.style.display = 'none';
                    previewImg.style.display = 'block';
                    previewImg.style.animation = 'none';
                    void previewImg.offsetHeight;
                    previewImg.style.animation = 'fadeIn 0.4s ease';
                    questionBox.hidden = false;
                    enterBtn.onclick = () => {
                        fitMapBounds(itemEl?._mapBounds || dataBboxToMapBounds(selected));
                        openChatModal(fileName, imgUrl, spatialCtx);
                    };
                };

                if (imagerySource === 'sentinel2') {
                    progressBar.style.display = 'none';
                    progressBar.hidden = true;
                    showReadyImage();
                    showToast('近期公开影像已生成', 'success');
                } else {
                    setRegionStage(itemEl, 'downloading', '瓦片下载中');
                    status.innerHTML = '<span class="spinner"></span> 正在下载瓦片...';
                    progressBar.hidden = false;
                    progressText.textContent = `0/${totalTiles}`;
                    await pollDownloadProgress(fileName, totalTiles, {
                        ownerEl: itemEl,
                        progressBar,
                        fill,
                        progressText,
                        statusEl: status,
                        onReady: showReadyImage
                    });
                }
            } else {
                setRegionStage(itemEl, 'error', '影像失败');
                status.innerHTML = '抓取失败：' + d.msg;
                showToast('抓取失败：' + d.msg, 'error');
            }
        } catch (e) {
            setRegionStage(itemEl, 'error', '网络失败');
            status.innerHTML = '网络错误，请检查后端';
            showToast('网络请求失败，请确认后端运行中', 'error');
        }
    }

    // ==========================================
    // 对话舱
    // ==========================================
    const modal = document.getElementById('chat-modal');
    const closeBtn = document.getElementById('close-chat-btn');
    const sendBtn = document.getElementById('chat-send-btn');
    const textarea = document.getElementById('chat-textarea');
    const chatBox = document.getElementById('chat-message-box');
    let modalImg = document.getElementById('chat-modal-img');
    const modalIdSpan = document.getElementById('chat-modal-id');
    const agentRegionBtn = document.getElementById('agent-region-btn');

    if (agentRegionBtn) {
        agentRegionBtn.addEventListener('click', () => {
            if (!currentActiveImage || currentActiveImage === '__compare__') {
                showToast('请先打开一个已生成影像的区域', 'warning');
                return;
            }
            const data = getChatData(currentActiveImage);
            const goal = textarea.value.trim() || `请对当前框选区域进行${data.scene?.source === 'sentinel2' ? '近期态势' : '遥感'}智能调查`;
            if (agentGoalInput) agentGoalInput.value = goal;
            startAgentSession({
                goal,
                file_name: currentActiveImage,
                scene_id: data.sceneId,
                bbox: data.bbox
            });
            showToast('已按当前区域启动 Agent 调查', 'info');
        });
    }

    function renderScenePanel(scene) {
        const panel = document.getElementById('imagery-scene-panel');
        if (!panel) return;
        if (!scene) {
            panel.hidden = true;
            panel.innerHTML = '';
            return;
        }

        const gsd = scene.gsd_m ? `约 ${scene.gsd_m} m/像素` : '未知';
        const cloud = scene.cloud_percent != null ? `${scene.cloud_percent}%` : '未知';
        const acquiredAt = formatSceneDate(scene.acquired_at);
        const grade = sceneGradeText(scene.decision_grade);
        const limitations = scene.limitations || '公开影像辅助筛查';
        const selection = scene.selection;
        const selectionTitle = selection
            ? `${selection.summary || '已记录候选优选依据'}${
                Array.isArray(selection.score_reasons) && selection.score_reasons.length
                    ? `：${selection.score_reasons.slice(0, 3).join('；')}`
                    : ''
            }`
            : '';
        panel.innerHTML = `
            <div class="scene-compact-head">
                <span>${escapeHtml(scene.source_label || scene.source || '影像源')}</span>
                <strong title="${escapeHtml(limitations)}">${escapeHtml(grade)}</strong>
            </div>
            <div class="scene-chip-row">
                <span title="拍摄日期"><i class="ri-calendar-line" aria-hidden="true"></i>${escapeHtml(acquiredAt)}</span>
                <span title="空间分辨率"><i class="ri-ruler-line" aria-hidden="true"></i>${escapeHtml(gsd)}</span>
                <span title="云量"><i class="ri-cloudy-line" aria-hidden="true"></i>${escapeHtml(cloud)}</span>
                ${scene.processing_level ? `<span title="处理级别"><i class="ri-stack-line" aria-hidden="true"></i>${escapeHtml(scene.processing_level)}</span>` : ''}
            </div>
            ${selection ? `<button class="scene-mini-note" type="button" title="${escapeHtml(selectionTitle)}"><i class="ri-check-double-line" aria-hidden="true"></i>已选最佳候选</button>` : ''}
        `;
        panel.hidden = false;
    }

    function openChatModal(fileName, imgUrl, spatialCtx) {
        currentActiveImage = fileName;
        currentSpatialCtx = spatialCtx || "";
        clearTargetMarkers();            // 清掉上一张图遗留的地图标点
        restoreChatModalLayout();        // 若上次是对比模式,重建单图布局,避免白屏
        modalImg.src = imgUrl;
        modalIdSpan.innerText = currentSpatialCtx ? ` · ${currentSpatialCtx}` : '';
        renderScenePanel(getChatData(fileName).scene);
        renderChatHistory();
        modal.style.display = 'flex';
        updatePromptScene();
        setTimeout(() => textarea.focus(), 100);
    }

    closeBtn.onclick = () => { modal.style.display = 'none'; };

    // 点击背景关闭
    modal.addEventListener('click', (e) => {
        if (e.target === modal) modal.style.display = 'none';
    });

    // Enter 发送，Shift+Enter 换行
    textarea.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendBtn.click();
        }
    });

    // 输入框自适应高度
    function autoResize() {
        textarea.style.height = 'auto';
        textarea.style.height = textarea.scrollHeight + 'px';
    }
    textarea.addEventListener('input', autoResize);

    // Esc 关闭
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && modal.style.display === 'flex') {
            modal.style.display = 'none';
        }
    });

    // 预设提示词：根据单图/多图模式显示对应分组
    function updatePromptScene() {
        const isMulti = currentActiveImage === '__compare__';
        document.querySelectorAll('.prompt-group[data-scene]').forEach(g => {
            const scenes = g.dataset.scene.split(',');
            g.classList.toggle('visible', scenes.includes(isMulti ? 'multi' : 'single'));
        });
    }

    // 全局函数：展开/收起预设提示词面板
    window.togglePrompts = function() {
        const area = document.querySelector('.chat-prompts-area');
        const btn = document.getElementById('chat-prompt-toggle');
        const isOpen = area.classList.toggle('show');
        if (isOpen) {
            btn.classList.add('active');
            updatePromptScene();
        } else {
            btn.classList.remove('active');
        }
    };

    // 全局函数：选中预设提示词
    window.pickPrompt = function(tag) {
        const detail = tag.getAttribute('data-detail');
        if (detail) {
            const ta = document.getElementById('chat-textarea');
            ta.value = detail;
            ta.focus();
            ta.style.height = 'auto';
            ta.style.height = ta.scrollHeight + 'px';
        }
        document.querySelector('.chat-prompts-area').classList.remove('show');
        document.getElementById('chat-prompt-toggle').classList.remove('active');
        tag.classList.add('flash');
        setTimeout(() => tag.classList.remove('flash'), 400);
    };

    function getChatData(fileName) {
        const entry = chatMemories[fileName];
        return entry ? (Array.isArray(entry) ? { history: entry, spatial: "" } : entry) : { history: [], spatial: "" };
    }

    function renderMarkdown(text) {
        if (!text) return '';
        let html = text
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');

        // 表格处理（先做，内部单元格再补内联格式化）
        html = html.replace(/(\|[^\n]+\|\n\|[-:|\s]+\|\n(?:\|[^\n]+\|\n?)*)/gm, (match) => {
            const rows = match.trim().split('\n');
            let tableHtml = '<table class="md-table">';
            rows.forEach((row, i) => {
                const cells = row.split('|').filter(c => c.trim() !== '');
                const tag = i === 1 ? '' : (i === 0 ? 'th' : 'td');
                if (tag) {
                    tableHtml += '<tr>';
                    cells.forEach(c => {
                        let cellHtml = c.trim();
                        cellHtml = cellHtml.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
                        cellHtml = cellHtml.replace(/\*(.+?)\*/g, '<em>$1</em>');
                        cellHtml = cellHtml.replace(/`([^`]+)`/g, '<code class="md-code">$1</code>');
                        tableHtml += `<${tag}>${cellHtml}</${tag}>`;
                    });
                    tableHtml += '</tr>';
                }
            });
            tableHtml += '</table>';
            return tableHtml;
        });

        html = html.replace(/^#### (.+)$/gm, '<div class="md-h4">$1</div>');
        html = html.replace(/^### (.+)$/gm, '<div class="md-h3">$1</div>');
        html = html.replace(/^## (.+)$/gm, '<div class="md-h2">$1</div>');
        html = html.replace(/^# (.+)$/gm, '<div class="md-h1">$1</div>');

        html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
        html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
        html = html.replace(/`([^`]+)`/g, '<code class="md-code">$1</code>');

        const lines = html.split('\n');
        let result = [], inUl = false, inOl = false;
        for (let line of lines) {
            const ulMatch = line.match(/^[*-] (.+)$/);
            const olMatch = line.match(/^\d+\. (.+)$/);
            if (ulMatch) {
                if (inOl) { result.push('</ol>'); inOl = false; }
                if (!inUl) { result.push('<ul class="md-ul">'); inUl = true; }
                result.push(`<li>${ulMatch[1]}</li>`);
            } else if (olMatch) {
                if (inUl) { result.push('</ul>'); inUl = false; }
                if (!inOl) { result.push('<ol class="md-ol">'); inOl = true; }
                result.push(`<li>${olMatch[1]}</li>`);
            } else {
                if (inUl) { result.push('</ul>'); inUl = false; }
                if (inOl) { result.push('</ol>'); inOl = false; }
                if (line.trim() === '') {
                    result.push('<br>');
                } else if (!line.startsWith('<table') && !line.startsWith('<tr') && !line.startsWith('<td') && !line.startsWith('<th') && !line.startsWith('</table') && !line.startsWith('</tr') && !line.startsWith('</td') && !line.startsWith('</th') && !line.startsWith('<div') && !line.startsWith('<ul') && !line.startsWith('<ol') && !line.startsWith('<li') && !line.startsWith('</ul') && !line.startsWith('</ol') && !line.startsWith('</div') && !line.startsWith('<strong') && !line.startsWith('<em') && !line.startsWith('<br') && !line.startsWith('<code')) {
                    result.push(`<p>${line}</p>`);
                } else {
                    result.push(line);
                }
            }
        }
        if (inUl) result.push('</ul>');
        if (inOl) result.push('</ol>');
        return result.join('\n');
    }

    function renderMethodMeta(method) {
        if (!method || typeof method !== 'object') return '';
        const parts = [];
        const modeLabel = method.mode === 'fast' ? '快速模式' : '精准模式';
        if (method.model) parts.push(`${modeLabel} · ${method.model}`);
        if (method.task_label) parts.push(`任务：${method.task_label}`);
        if (method.confidence?.label) parts.push(`可信度：${method.confidence.label}`);
        const rec = method.source_recommendation;
        if (rec?.recommended_label) {
            const recText = rec.alignment === 'matched'
                ? `${rec.recommended_label}（匹配）`
                : `${rec.recommended_label}（建议切换）`;
            parts.push(recText);
        }
        if (!parts.length) return '';
        return `<div class="analysis-method-meta">${parts.map(escapeHtml).join(' / ')}</div>`;
    }

    function renderChatHistory() {
        chatBox.innerHTML = '';
        const data = getChatData(currentActiveImage);
        const history = data.history || [];
        if (history.length === 0) {
            chatBox.innerHTML = '<div class="chat-empty-state"><i class="ri-question-answer-line" aria-hidden="true"></i><span>可以开始提问了</span></div>';
        } else {
            history.forEach(msg => {
                const div = document.createElement('div');
                div.className = `chat-bubble ${msg.role === 'user' ? 'chat-user' : 'chat-ai'}`;
                if (msg.role === 'ai') {
                    div.innerHTML = renderMarkdown(msg.content) + renderMethodMeta(msg.analysis_method);
                } else {
                    div.textContent = msg.content;
                }
                chatBox.appendChild(div);
            });
        }
        chatBox.scrollTop = chatBox.scrollHeight;
    }

    // 把左侧恢复为单图布局。对比模式会清空 .chat-modal-left 导致 #chat-modal-img
    // 被移除、modalImg 指向游离节点;进入单图视图前必须先重建,否则设 src 不显示(白屏)。
    function restoreChatModalLayout() {
        const left = document.querySelector('.chat-modal-left');
        if (!document.getElementById('chat-modal-img')) {
            left.innerHTML = `
                <div class="imagery-preview-wrap">
                    <img id="chat-modal-img" src="" alt="卫星图放大版" draggable="false">
                </div>
                <div id="imagery-scene-panel" class="imagery-scene-panel" hidden></div>
            `;
        } else if (!document.getElementById('imagery-scene-panel')) {
            left.insertAdjacentHTML('beforeend', '<div id="imagery-scene-panel" class="imagery-scene-panel" hidden></div>');
        }
        left.style.flexWrap = '';
        left.style.flexDirection = '';
        left.style.gap = '';
        left.style.alignContent = '';
        modalImg = document.getElementById('chat-modal-img');   // 重新捕获引用
        modalImg.style.display = '';
    }

    // ==========================================
    // AI 定位目标 → 地图标点(坐标接地闭环)
    // ==========================================
    let targetMarkers = [];
    // 用 divIcon(纯 CSS 圆点),不依赖 Leaflet 默认图标资源,离线也能显示
    const aiTargetIcon = L.divIcon({
        className: 'ai-target-marker',
        html: '<div class="ai-target-dot"></div>',
        iconSize: [16, 16], iconAnchor: [8, 8]
    });
    function clearTargetMarkers() {
        targetMarkers.forEach(m => map.removeLayer(m));
        targetMarkers = [];
    }
    function placeTargetMarkers(targets) {
        clearTargetMarkers();
        if (!Array.isArray(targets)) return;
        targets.forEach(t => {
            if (typeof t.lat !== 'number' || typeof t.lng !== 'number') return;
            const m = L.marker([t.lat, t.lng], { icon: aiTargetIcon }).addTo(map);
            let popup = `<b>${t.label || 'AI 定位目标'}</b>`;
            if (t.width_m != null) popup += `<br>尺寸约 ${t.width_m}m × ${t.height_m}m`;
            if (t.area_m2 != null) popup += `<br>占地约 ${t.area_m2 >= 10000 ? (t.area_m2 / 10000).toFixed(2) + ' 公顷' : Math.round(t.area_m2) + ' m²'}`;
            popup += `<br>${t.lat}°N, ${t.lng}°E`;
            m.bindPopup(popup);
            targetMarkers.push(m);
        });
        if (targetMarkers.length) targetMarkers[targetMarkers.length - 1].openPopup();
    }

    function getAnalysisMode() {
        const mode = document.getElementById('model-select').value || 'precise';
        if (mode === 'fast') {
            return {
                mode,
                label: '快速模式',
                model: 'qwen3-vl-flash',
                activePerception: false
            };
        }
        return {
            mode: 'precise',
            label: '精准模式',
            model: 'qwen3-vl-plus',
            activePerception: true
        };
    }

    async function checkSourceRecommendation(question, currentSource) {
        try {
            const res = await fetch('/api/imagery/recommend-source/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ question, current_source: currentSource })
            });
            const result = await res.json();
            const rec = result.data?.recommendation;
            if (result.code === 200 && rec?.alignment === 'switch_recommended') {
                showToast(`${rec.recommended_label}：${rec.action}`, 'info');
            }
            return rec || null;
        } catch (e) {
            return null;
        }
    }

    let isQuerying = false;
    sendBtn.onclick = async () => {
        if (isQuerying) return;          // 防止分析期间重复提交
        const text = textarea.value.trim();
        if (!text) return;

        isQuerying = true;
        sendBtn.disabled = true;
        modal.classList.add('is-querying');

        const data = getChatData(currentActiveImage);
        data.history.push({ role: 'user', content: text });
        textarea.value = '';
        autoResize();
        renderChatHistory();

        const isCompare = currentActiveImage === '__compare__';
        const analysisMode = getAnalysisMode();
        const loadingDiv = document.createElement('div');
        loadingDiv.className = 'chat-bubble chat-ai';
        loadingDiv.innerHTML = `<span class="spinner"></span> SatelliteSense ${analysisMode.label}${isCompare ? '正在对比分析' : '正在分析'}...`;
        chatBox.appendChild(loadingDiv);
        chatBox.scrollTop = chatBox.scrollHeight;

        try {
            let sourceRecommendation = null;
            if (!isCompare) {
                const currentSource = data.scene?.source || getImagerySource();
                sourceRecommendation = await checkSourceRecommendation(text, currentSource);
            }
            const body = isCompare
                ? { file_names: data.compareFiles, question: text, history: data.history.slice(0, -1), model: analysisMode.model, mode: analysisMode.mode }
                : { file_name: currentActiveImage, scene_id: data.sceneId, question: text, history: data.history.slice(0, -1), spatial_context: currentSpatialCtx, model: analysisMode.model, mode: analysisMode.mode, active_perception: analysisMode.activePerception, gsd: data.gsd, bbox: data.bbox, source_recommendation: sourceRecommendation };
            const res = await fetch("/api/ai/query-region/", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body)
            });
            const result = await res.json();

            loadingDiv.remove();

            if (result.code === 200) {
                let aiContent = result.data.answer;
                if (result.data.active_stages >= 2) {
                    aiContent = `🔍 主动感知（${result.data.active_stages}级分析）\n\n` + aiContent;
                }
                if (result.data.scene) data.scene = result.data.scene;
                data.history.push({
                    role: 'ai',
                    content: aiContent,
                    analysis_method: result.data.analysis_method || null
                });
                if (!isCompare) placeTargetMarkers(result.data.targets);  // 把 AI 定位目标标到地图
            } else {
                data.history.push({ role: 'ai', content: "❌ 分析失败: " + result.msg });
                showToast('AI 分析失败', 'error');
            }
        } catch(e) {
            loadingDiv.remove();
            data.history.push({ role: 'ai', content: "⚠️ 网络请求异常，请检查后端状态" });
            showToast('网络请求异常', 'error');
        } finally {
            isQuerying = false;
            sendBtn.disabled = false;
            modal.classList.remove('is-querying');
        }
        renderChatHistory();
        if (currentActiveImage) saveHistory(currentActiveImage);
        textarea.focus();
    };

    // ==========================================
    // 局部放大分析（拖拽框选，独立画布不干扰 UI）
    // ==========================================
    const zoomBtn = document.getElementById('zoom-btn');
    let zoomMode = false;
    let zoomCanvas = null, zStart = null, zRect = null;

    function ensureZoomContainer() {
        const left = document.querySelector('.chat-modal-left');
        if (left.style.position !== 'relative') left.style.position = 'relative';
        return left;
    }

    zoomBtn.addEventListener('click', () => {
        if (currentActiveImage === '__compare__' || !currentActiveImage) return;
        zoomMode = !zoomMode;
        zoomBtn.style.background = zoomMode ? 'rgba(0,122,255,0.25)' : 'rgba(255,255,255,0.06)';
        zoomBtn.style.color = zoomMode ? '#007aff' : 'rgba(255,255,255,0.7)';
        zoomBtn.innerHTML = zoomMode
            ? '<i class="ri-close-line" aria-hidden="true"></i>退出'
            : '<i class="ri-zoom-in-line" aria-hidden="true"></i>放大';

        if (zoomMode) {
            const container = ensureZoomContainer();
            zoomCanvas = document.createElement('div');
            zoomCanvas.id = 'zoom-canvas';
            container.appendChild(zoomCanvas);
        } else {
            if (zoomCanvas) { zoomCanvas.remove(); zoomCanvas = null; }
            zStart = null; zRect = null;
        }
    });

    document.addEventListener('mousedown', (e) => {
        if (!zoomMode || !zoomCanvas || !e.target.closest('#zoom-canvas')) return;
        e.preventDefault();
        const r = zoomCanvas.getBoundingClientRect();
        zStart = { x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height };
        zRect = document.createElement('div');
        zRect.className = 'zoom-selection-rect';
        zoomCanvas.appendChild(zRect);
    });

    document.addEventListener('mousemove', (e) => {
        if (!zStart || !zRect || !zoomCanvas) return;
        const r = zoomCanvas.getBoundingClientRect();
        const cx = (e.clientX - r.left) / r.width;
        const cy = (e.clientY - r.top) / r.height;
        const x1 = Math.min(zStart.x, cx) * 100, y1 = Math.min(zStart.y, cy) * 100;
        const w = Math.abs(cx - zStart.x) * 100, h = Math.abs(cy - zStart.y) * 100;
        zRect.style.left = x1 + '%';
        zRect.style.top = y1 + '%';
        zRect.style.width = w + '%';
        zRect.style.height = h + '%';
    });

    document.addEventListener('mouseup', async (e) => {
        if (!zStart || !zRect || !zoomCanvas) return;
        const r = zoomCanvas.getBoundingClientRect();
        const ex = (e.clientX - r.left) / r.width;
        const ey = (e.clientY - r.top) / r.height;
        const clamp01 = (v) => Math.max(0, Math.min(1, v));
        const x1 = clamp01(Math.min(zStart.x, ex)), x2 = clamp01(Math.max(zStart.x, ex));
        const y1 = clamp01(Math.min(zStart.y, ey)), y2 = clamp01(Math.max(zStart.y, ey));
        zStart = null; zRect = null;

        if (zoomCanvas) { zoomCanvas.remove(); zoomCanvas = null; }
        zoomMode = false;
        zoomBtn.style.background = 'rgba(255,255,255,0.06)';
        zoomBtn.style.color = 'rgba(255,255,255,0.7)';
        zoomBtn.innerHTML = '<i class="ri-zoom-in-line" aria-hidden="true"></i>放大';

        if (x2 - x1 < 0.03 || y2 - y1 < 0.03) return;

        const data = getChatData(currentActiveImage);
        const bbox = data.bbox;
        if (!bbox) { showToast('缺少坐标信息', 'error'); return; }

        const subMinLng = bbox.min_lng + (bbox.max_lng - bbox.min_lng) * x1;
        const subMaxLng = bbox.min_lng + (bbox.max_lng - bbox.min_lng) * x2;
        const latSpan = bbox.max_lat - bbox.min_lat;
        const subMinLat = bbox.max_lat - latSpan * y2;
        const subMaxLat = bbox.max_lat - latSpan * y1;

        showToast('正在获取放大影像...', 'info');
        try {
            const r2 = await fetch("/api/satellite/get-img/", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ min_lng: subMinLng, max_lng: subMaxLng, min_lat: subMinLat, max_lat: subMaxLat })
            });
            const d = await r2.json();
            if (d.code === 200) {
                const fileName = d.data.file_name;
                const totalTiles = d.data.total_tiles || 1;
                const imgUrl = `/api/satellite/show-img/?file=${fileName}`;
                const spatialCtx = d.data.gsd_m ? `范围: ${d.data.area_km2} km² | 分辨率: ${d.data.gsd_m} m/像素` : "";
                chatMemories[fileName] = {
                    history: [],
                    spatial: spatialCtx,
                    bbox: { min_lng: subMinLng, max_lng: subMaxLng, min_lat: subMinLat, max_lat: subMaxLat },
                    gsd: d.data.gsd_m,
                    sceneId: d.data.scene_id,
                    scene: d.data.scene
                };
                modalIdSpan.innerText = spatialCtx ? ` · ${spatialCtx}` : '';
                currentActiveImage = fileName;
                currentSpatialCtx = spatialCtx;
                renderScenePanel(d.data.scene);
                await pollDownloadProgress(fileName, totalTiles, {
                    onReady: () => {
                        modalImg.src = imgUrl + '&t=' + Date.now();
                        renderChatHistory();
                    },
                    successMessage: '放大影像已加载'
                });
            } else {
                showToast('放大影像获取失败：' + d.msg, 'error');
            }
        } catch(err) {
            showToast('放大影像获取失败', 'error');
        }
    });

    // ==========================================
    // 地点搜索（Nominatim 实时地理编码）
    // ==========================================
    const searchInput = document.getElementById('search-input');
    const searchResults = document.getElementById('search-results');
    let searchTimeout = null;
    let searchIdx = -1;

    searchInput.addEventListener('input', () => {
        clearTimeout(searchTimeout);
        const q = searchInput.value.trim();
        if (q.length === 0) { searchResults.style.display = 'none'; searchIdx = -1; return; }
        searchResults.style.display = 'block';
        searchResults.innerHTML = '<div class="search-result-item search-state"><span class="spinner"></span> 搜索中...</div>';
        searchIdx = -1;
        searchTimeout = setTimeout(async () => {
            try {
                const r = await fetch(`/api/geo/search/?q=${encodeURIComponent(q)}`);
                const resp = await r.json();
                const data = resp.data || [];
                searchResults.innerHTML = '';
                if (data.length === 0) {
                    searchResults.innerHTML = '<div class="search-result-item search-state">无匹配结果</div>';
                } else {
                    data.forEach((item, i) => {
                        const name = item.name || item.display_name.split(',')[0];
                        const div = document.createElement('div');
                        div.className = 'search-result-item';
                        div.innerHTML = `<div class="name">${name}</div><div class="detail">${item.display_name}</div>`;
                        div.addEventListener('mousedown', (e) => {
                            e.preventDefault();
                            map.flyTo([item.lat, item.lon], 14, { duration: 1.2 });
                            searchInput.value = name;
                            searchResults.style.display = 'none';
                            searchIdx = -1;
                            showToast('已定位: ' + name, 'info');
                        });
                        searchResults.appendChild(div);
                    });
                }
                searchResults.style.display = 'block';
            } catch(e) {
                searchResults.innerHTML = '<div class="search-result-item search-state">搜索服务不可用</div>';
                searchResults.style.display = 'block';
            }
        }, 250);
    });

    searchInput.addEventListener('keydown', (e) => {
        const items = searchResults.querySelectorAll('.search-result-item');
        if (e.key === 'Escape') { searchResults.style.display = 'none'; searchIdx = -1; searchInput.blur(); return; }
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            searchIdx = Math.min(searchIdx + 1, items.length - 1);
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            searchIdx = Math.max(searchIdx - 1, 0);
        } else if (e.key === 'Enter') {
            e.preventDefault();
            if (searchIdx >= 0 && items[searchIdx]) {
                items[searchIdx].dispatchEvent(new Event('mousedown', { bubbles: true }));
            }
            return;
        } else { return; }
        items.forEach((item, i) => item.style.background = i === searchIdx ? 'rgba(0,122,255,0.2)' : '');
    });

    document.addEventListener('mousedown', (e) => {
        if (!e.target.closest('#search-box')) { searchResults.style.display = 'none'; searchIdx = -1; }
    });

    searchInput.addEventListener('focus', () => {
        if (searchInput.value.trim().length > 0) searchResults.style.display = 'block';
    });

    // ==========================================
    // 图层切换
    // ==========================================
    const adminToggle = document.getElementById('layer-admin');
    const roadsToggle = document.getElementById('layer-roads');
    const labelLayer = L.tileLayer('https://a.basemaps.cartocdn.com/light_only_labels/{z}/{x}/{y}.png', {
        maxZoom: 18, attribution: '&copy; <a href="https://carto.com/">CARTO</a>'
    });

    adminToggle.addEventListener('change', () => {
        if (provinceLayer) {
            adminToggle.checked ? provinceLayer.addTo(map) : map.removeLayer(provinceLayer);
        }
    });

    function applyRoadLabels() {
        if (roadsToggle.checked) {
            if (!map.hasLayer(labelLayer)) labelLayer.addTo(map);
        } else {
            if (map.hasLayer(labelLayer)) map.removeLayer(labelLayer);
        }
    }

    roadsToggle.addEventListener('change', applyRoadLabels);

    const origStyleChange = styleToggle.onchange;
    styleToggle.addEventListener('change', (e) => {
        if (origStyleChange) origStyleChange.call(styleToggle, e);
        applyRoadLabels();
    });

    // ==========================================
    // 快捷键
    // ==========================================
    document.addEventListener('keydown', (e) => {
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') {
            if (e.key === 'Enter' && e.ctrlKey) {
                e.preventDefault();
                sendBtn.click();
            }
            return;
        }
        if (e.key === 'Escape') {
            let cancelledSel = false;
            if (selectionRect) { map.removeLayer(selectionRect); selectionRect = null; cancelledSel = true; }
            if (isSelecting) { isSelecting = false; map.dragging.enable(); cancelledSel = true; }
            if (modal.style.display === 'flex') { modal.style.display = 'none'; }
            // 只提示真正取消了的"框选";不谎称能中断已发起的下载/AI 请求
            if (cancelledSel) showToast('已取消框选', 'info');
        }
    });

    // ==========================================
    // 聊天历史
    // ==========================================
    const historyList = document.getElementById('history-list');
    const refreshHistoryBtn = document.getElementById('refresh-history-btn');

    async function loadHistories() {
        try {
            const r = await fetch('/api/ai/history/');
            const d = await r.json();
            if (d.code !== 200 || !d.data.length) {
                historyList.innerHTML = '<div class="history-empty">暂无历史记录</div>';
                return;
            }
            historyList.innerHTML = '';
            d.data.forEach(h => {
                const item = document.createElement('div');
                item.className = 'history-item';
                item.title = '点击加载历史对话';
                const badge = document.createElement('span');
                badge.className = 'history-source-badge';
                badge.textContent = historySourceCode(h);
                const label = document.createElement('span');
                label.className = 'history-main';
                label.innerHTML = `
                    <b>${escapeHtml(historyLabel(h))}</b>
                    <small>${escapeHtml(historySubtitle(h))}</small>
                `;
                const time = document.createElement('time');
                time.textContent = formatCompactDate(h.updated_at || h.created_at);
                item.addEventListener('click', async () => {
                    const rr = await fetch(`/api/ai/history/${h.id}/`);
                    const dd = await rr.json();
                    if (dd.code === 200) {
                        const fileName = dd.data.image_file;
                        const imgUrl = `/api/satellite/show-img/?file=${fileName}`;
                        currentActiveImage = fileName;
                        currentSpatialCtx = dd.data.spatial_context || '';
                        chatMemories[fileName] = {
                            history: dd.data.messages || [],
                            spatial: dd.data.spatial_context || '',
                            bbox: dd.data.bbox,
                            gsd: dd.data.scene?.gsd_m,
                            sceneId: dd.data.scene_id,
                            scene: dd.data.scene
                        };
                        restoreChatModalLayout();        // 重建单图布局,避免对比模式残留导致白屏
                        modalImg.src = imgUrl;
                        modalIdSpan.innerText = currentSpatialCtx ? ' · ' + currentSpatialCtx : '';
                        renderScenePanel(dd.data.scene);
                        renderChatHistory();
                        modal.style.display = 'flex';
                        if (dd.data.bbox) {
                            const b = dd.data.bbox;
                            fitMapBounds(dataBboxToMapBounds(b));
                        }
                        showToast('已加载历史对话', 'info');
                    } else if (dd.code === 410) {
                        showToast(dd.msg || '历史影像文件已丢失', 'error');
                        loadHistories();
                    }
                });
                const delBtn = document.createElement('button');
                delBtn.className = 'history-delete';
                delBtn.type = 'button';
                delBtn.title = '删除历史';
                delBtn.innerHTML = '<i class="ri-close-line" aria-hidden="true"></i>';
                delBtn.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    await fetch(`/api/ai/history/${h.id}/`, { method: 'DELETE' });
                    loadHistories();
                });
                item.appendChild(badge);
                item.appendChild(label);
                item.appendChild(time);
                item.appendChild(delBtn);
                historyList.appendChild(item);
            });
        } catch (e) {}
    }
    refreshHistoryBtn.addEventListener('click', loadHistories);

    async function saveHistory(fileName) {
        if (!fileName || fileName === '__compare__') return;
        const mem = chatMemories[fileName] || {};
        if (!Array.isArray(mem.history) || mem.history.length === 0) return;
        const bbox = mem.bbox ? mem.bbox : {};
        try {
            const r = await fetch('/api/ai/history/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    image_file: fileName,
                    scene_id: mem.sceneId,
                    messages: mem.history || [],
                    spatial_context: mem.spatial || '',
                    bbox: bbox
                })
            });
            const d = await r.json();
            if (d.code === 410) {
                showToast(d.msg || '历史影像文件已丢失', 'error');
            }
            loadHistories();
        } catch (e) {}
    }

    // ==========================================
    // 报告导出
    // ==========================================
    document.getElementById('report-btn').addEventListener('click', async () => {
        if (!currentActiveImage) return;
        const mem = chatMemories[currentActiveImage] || {};
        showToast('正在生成报告...', 'info');
        try {
            const r = await fetch('/api/report/generate/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    file_name: currentActiveImage,
                    scene_id: mem.sceneId,
                    title: 'SatelliteSense 遥感分析报告',
                    messages: mem.history || [],
                    spatial_context: mem.spatial || '',
                    bbox: mem.bbox || {}
                })
            });
            const d = await r.json();
            if (d.code === 200) {
                window.location.href = d.data.download_url;
                showToast('报告已生成，正在下载', 'success');
            } else if (d.code === 410) {
                showToast(d.msg || '卫星图文件已丢失，请重新框选', 'error');
                loadHistories();
            } else {
                showToast('报告生成失败：' + d.msg, 'error');
            }
        } catch (e) {
            showToast('报告生成失败', 'error');
        }
    });

    // 聊天发送后自动保存（sendBtn.onclick 里 AI 回复后调用）

    loadHistories();

    initMapLayers();
});
