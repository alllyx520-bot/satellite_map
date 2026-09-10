document.addEventListener('DOMContentLoaded', () => {
    // CDN 兜底:Leaflet 不可达时给可读提示,而非整页脚本静默崩溃留下空白地图
    if (typeof L === 'undefined') {
        const m = document.getElementById('map');
        if (m) m.innerHTML = '<div style="display:grid;place-items:center;height:100%;color:#E4B36A;font:500 15px/1.7 system-ui,sans-serif;text-align:center;padding:24px">地图组件未能加载（地图库 CDN 不可达），请检查网络后刷新页面。</div>';
        return;
    }
    const MAP_MAX_ZOOM = 18;
    const SATELLITE_MAX_NATIVE_ZOOM = 16;
    const FIT_BOUNDS_MAX_ZOOM = 16;

    const normalMap = L.tileLayer('https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}', {
        subdomains: ["1", "2", "3", "4"],
        maxZoom: MAP_MAX_ZOOM,
        errorTileUrl: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=',
        attribution: '&copy; 高德地图'
    });

    const satelliteMap = L.tileLayer('https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}', {
        subdomains: ["1", "2", "3", "4"],
        maxZoom: MAP_MAX_ZOOM,
        maxNativeZoom: SATELLITE_MAX_NATIVE_ZOOM,
        errorTileUrl: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=',
        attribution: '&copy; 高德地图(卫星)'
    });

    // GIBS 每日产品有日级延迟，取 UTC 昨天
    function gibsDefaultDate() {
        return new Date(Date.now() - 86400000).toISOString().slice(0, 10);
    }
    const gibsUrl = (date) => `https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/MODIS_Terra_CorrectedReflectance_TrueColor/default/${date}/GoogleMapsCompatible_Level9/{z}/{y}/{x}.jpg`;
    let gibsDate = gibsDefaultDate();
    const gibsMap = L.tileLayer(gibsUrl(gibsDate), {
        maxZoom: MAP_MAX_ZOOM,
        maxNativeZoom: 9,
        errorTileUrl: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=',
        attribution: '&copy; NASA GIBS'
    });
    // 昨日产品偶尔尚未发布（404），整体回退到再前一天
    gibsMap.on('tileerror', () => {
        if (gibsDate === gibsDefaultDate()) {
            gibsDate = new Date(Date.now() - 2 * 86400000).toISOString().slice(0, 10);
            gibsMap.setUrl(gibsUrl(gibsDate));
        }
    });

    // 当前底图坐标系：高德底图为 GCJ-02，GIBS 为 WGS84（框选换算据此分支）
    let activeBasemap = 'satellite';
    let gibsHidProvince = false;
    function isGcjBasemap() {
        return activeBasemap !== 'gibs';
    }

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
        if (!isGcjBasemap()) return { mapBbox, dataBbox: mapBbox };
        const dataBbox = bboxFromPoints(bboxCorners(mapBbox).map(p => gcj02ToWgs84(p.lng, p.lat)));
        return { mapBbox, dataBbox };
    }

    function dataBboxToMapBounds(bbox) {
        if (!isGcjBasemap()) return [[bbox.min_lat, bbox.min_lng], [bbox.max_lat, bbox.max_lng]];
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
        mapResizeTimer = setTimeout(() => { applyWorkbenchLayout(); syncMapViewport(); }, 120);
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
    // 窄屏下左右两栏(各最大 340px 叠层)物理上无法并存,触发手风琴/默认收起,避免重叠。桌面布局不受影响。
    // innerWidth 为 0 时(初始化或渲染器未合成)不能判为窄屏,否则会把窄屏默认永久写进 localStorage。
    const narrowLayout = () => {
        const w = window.innerWidth;
        return Number.isFinite(w) && w > 0 && w <= 960;  // 与 CSS 响应式断点(max-width: 960px)一致
    };
    // 1280–1440 这类紧凑桌面：两侧面板默认收窄，保证地图(主内容)仍占足够宽度
    const compactDesktop = () => {
        const w = window.innerWidth;
        return Number.isFinite(w) && w > 960 && w <= 1440;
    };

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
        let agentCollapsed = Boolean(layout.agentCollapsed);
        let workspaceCollapsed = Boolean(layout.workspaceCollapsed);
        if (narrowLayout() && !agentCollapsed && !workspaceCollapsed) {
            // 响应式默认：本次会话收起即可，不落盘——否则一次窄屏会永久收掉桌面端右栏
            workspaceCollapsed = true;
        }
        const agentWidth = clamp(Number(layout.agentWidth) || (compactDesktop() ? 330 : 360), 300, 560);
        const workspaceWidth = clamp(Number(layout.workspaceWidth) || (compactDesktop() ? 348 : 388), 320, 560);
        document.documentElement.style.setProperty('--agent-sidebar-width', `${agentWidth}px`);
        document.documentElement.style.setProperty('--workspace-sidebar-width', `${workspaceWidth}px`);
        document.body.classList.toggle('agent-collapsed', agentCollapsed);
        document.body.classList.toggle('workspace-collapsed', workspaceCollapsed);
        if (agentCollapseBtn) {
            agentCollapseBtn.title = agentCollapsed ? '展开左侧 Agent' : '收起左侧 Agent';
            agentCollapseBtn.innerHTML = `<i class="${agentCollapsed ? 'ri-arrow-right-s-line' : 'ri-arrow-left-s-line'}" aria-hidden="true"></i>`;
        }
        if (workspaceCollapseBtn) {
            workspaceCollapseBtn.title = workspaceCollapsed ? '展开右侧工作区' : '收起右侧工作区';
            workspaceCollapseBtn.innerHTML = `<i class="${workspaceCollapsed ? 'ri-arrow-left-s-line' : 'ri-arrow-right-s-line'}" aria-hidden="true"></i>`;
        }
        setTimeout(syncMapViewport, 220);
    }

    function startSidebarResize(kind, event) {
        event.preventDefault();
        const isAgent = kind === 'agent';
        const maxWidth = Math.min(560, Math.max(320, window.innerWidth - 560));
        document.body.classList.add('is-resizing-sidebar');
        let lastWidth = null;
        const onMove = (moveEvent) => {
            lastWidth = isAgent
                ? clamp(moveEvent.clientX, 300, maxWidth)
                : clamp(window.innerWidth - moveEvent.clientX, 320, maxWidth);
            document.documentElement.style.setProperty(
                isAgent ? '--agent-sidebar-width' : '--workspace-sidebar-width',
                `${lastWidth}px`
            );
            document.body.classList.toggle(isAgent ? 'agent-collapsed' : 'workspace-collapsed', false);
            syncMapViewport();
        };
        const onUp = () => {
            document.body.classList.remove('is-resizing-sidebar');
            window.removeEventListener('mousemove', onMove);
            window.removeEventListener('mouseup', onUp);
            // 拖拽中只改 CSS 变量，结束时才落盘 localStorage
            if (lastWidth != null) {
                saveWorkbenchLayout(isAgent ? { agentWidth: lastWidth, agentCollapsed: false } : { workspaceWidth: lastWidth, workspaceCollapsed: false });
            }
            syncMapViewport();
        };
        window.addEventListener('mousemove', onMove);
        window.addEventListener('mouseup', onUp);
    }

    applyWorkbenchLayout();
    agentCollapseBtn?.addEventListener('click', () => {
        const collapsed = !document.body.classList.contains('agent-collapsed');
        const patch = { agentCollapsed: collapsed };
        if (narrowLayout() && !collapsed) patch.workspaceCollapsed = true;  // 窄屏手风琴:展开一栏即收起另一栏
        saveWorkbenchLayout(patch);
        applyWorkbenchLayout();
    });
    workspaceCollapseBtn?.addEventListener('click', () => {
        const collapsed = !document.body.classList.contains('workspace-collapsed');
        const patch = { workspaceCollapsed: collapsed };
        if (narrowLayout() && !collapsed) patch.agentCollapsed = true;
        saveWorkbenchLayout(patch);
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
            const menu = control._menu || control.querySelector('.toolbar-select-menu');
            if (trigger) trigger.setAttribute('aria-expanded', 'false');
            if (menu) {
                menu.hidden = true;
                // 窄屏下菜单被 portal 到 body（见 openMenu）：关闭时收回原 shell，避免 DOM 泄漏
                if (menu.classList.contains('is-portaled')) {
                    menu.classList.remove('is-portaled');
                    menu.style.left = '';
                    menu.style.top = '';
                    control._shell?.appendChild(menu);
                }
            }
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
                if (option.title) button.title = option.title;
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
                control._menu = menu;
                control._shell = shell;
                // ≤960px 顶栏 overflow-x:auto 会裁剪绝对定位下拉：
                // 移到 body 下改 position:fixed，按 trigger 视口坐标定位
                //（.glass 的 backdrop-filter 会把 fixed 劫持为相对顶栏，故必须移出 DOM）
                if (narrowLayout()) {
                    const r = trigger.getBoundingClientRect();
                    document.body.appendChild(menu);
                    menu.classList.add('is-portaled');
                    menu.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 226)) + 'px';
                    menu.style.top = (r.bottom + 8) + 'px';
                }
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
            const value = e.target.value;
            [normalMap, satelliteMap, gibsMap].forEach((layer) => {
                if (map.hasLayer(layer)) map.removeLayer(layer);
            });
            activeBasemap = value;
            if (value === 'gibs') {
                gibsMap.addTo(map);
                // 行政区 GeoJSON 是 GCJ-02，与 WGS84 的 GIBS 底图错位：暂隐并记录，切回 GCJ 底图恢复
                if (provinceLayer && map.hasLayer(provinceLayer)) {
                    map.removeLayer(provinceLayer);
                    gibsHidProvince = true;
                }
            } else {
                (value === 'satellite' ? satelliteMap : normalMap).addTo(map);
                if (gibsHidProvince) {
                    gibsHidProvince = false;
                    if (provinceLayer && document.getElementById('layer-admin')?.checked !== false) {
                        provinceLayer.addTo(map);
                    }
                }
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
        setTimeout(() => {
            toast.classList.add('out');
            setTimeout(() => { if (toast.parentNode) toast.remove(); }, 420);
        }, 3000);
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

    // 影像源配置表：key 同时是 toggle value 与后端 scene.source 的约定值
    const IMAGERY_SOURCE_CONFIG = {
        mapbox: {
            label: '高清底图',
            historyPrefix: '高清底图',
            endpoint: '/api/satellite/get-img/',
            collection: null,
            skipTileProgress: false,
            badge: 'HD'
        },
        tianditu: {
            label: '高清底图·天地图',
            historyPrefix: '高清底图·天地图',
            endpoint: '/api/satellite/get-img/',
            collection: null,
            basemapSource: 'tianditu',
            skipTileProgress: false,
            badge: 'TD'
        },
        esri: {
            label: '高清底图·Esri',
            historyPrefix: '高清底图·Esri',
            endpoint: '/api/satellite/get-img/',
            collection: null,
            basemapSource: 'esri',
            skipTileProgress: false,
            badge: 'ES'
        },
        sentinel2: {
            label: '近期公开影像',
            historyPrefix: '近期公开影像',
            endpoint: '/api/satellite/get-sentinel-img/',
            collection: 'sentinel-2-l2a',
            skipTileProgress: true,
            badge: 'S2'
        },
        sentinel1: {
            label: '雷达影像',
            historyPrefix: '雷达·全天候',
            endpoint: '/api/satellite/get-sentinel-img/',
            collection: 'sentinel-1-grd',
            skipTileProgress: true,
            badge: 'S1'
        },
        copdem: {
            label: '地形 DEM',
            historyPrefix: '地形·DEM',
            endpoint: '/api/satellite/get-sentinel-img/',
            collection: 'cop-dem-glo-30',
            skipTileProgress: true,
            badge: 'DEM'
        }
    };

    function imagerySourceConfig(source) {
        return IMAGERY_SOURCE_CONFIG[source] || null;
    }

    function sceneBrief(scene) {
        if (!scene) return '';
        const parts = [scene.source_label || scene.source || '影像源', sceneGradeText(scene.decision_grade)];
        if (imagerySourceConfig(scene.source)?.collection) {
            const acquired = scene.acquired_at ? formatSceneDate(scene.acquired_at).split(' ')[0] : '';
            if (acquired) parts.push(acquired);
            if (scene.cloud_percent != null) parts.push(`云量 ${scene.cloud_percent}%`);
            if (scene.selection?.suitability_score != null) parts.push(`评分 ${scene.selection.suitability_score}`);
        }
        return parts.filter(Boolean).join(' · ');
    }

    function historyLabel(history) {
        const scene = history.scene;
        // 截断后清尾部悬挂分隔符（"· "、" / "等），避免标题以孤立符号结尾
        const trimTail = (s) => s.replace(/[\s·/|-]+$/, '');
        if (!scene) return trimTail((history.spatial_context || history.image_file).substring(0, 36));
        const prefix = imagerySourceConfig(scene.source)?.historyPrefix || '高清底图';
        const acquired = scene.acquired_at ? formatSceneDate(scene.acquired_at).split(' ')[0] : '';
        const detail = imagerySourceConfig(scene.source)?.collection
            ? [acquired, scene.cloud_percent != null ? `云量${scene.cloud_percent}%` : '', scene.selection?.suitability_score != null ? `评分${scene.selection.suitability_score}` : ''].filter(Boolean).join(' / ')
            : (history.spatial_context || scene.decision_grade_label || sceneGradeText(scene.decision_grade));
        return trimTail(`${prefix}${detail ? ' · ' + detail : ''}`.substring(0, 42));
    }

    function historySubtitle(history) {
        const parts = [];
        if (history.spatial_context) parts.push(history.spatial_context);
        if (history.scene) parts.push(sceneBrief(history.scene));
        if (!parts.length && history.image_file) parts.push(history.image_file);
        return parts.filter(Boolean).join(' / ').substring(0, 88).replace(/[\s·/|-]+$/, '');
    }

    function historySourceCode(history) {
        return imagerySourceConfig(history.scene?.source)?.badge || 'IMG';
    }

    function getImagerySource() {
        const value = imagerySourceToggle?.value;
        return IMAGERY_SOURCE_CONFIG[value] ? value : 'mapbox';
    }

    function imagerySourceLabel(source) {
        return imagerySourceConfig(source)?.label || '高清底图';
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
    async function showDependencyHint() {
        try {
            const res = await fetch('/api/system/dependencies/');
            const body = await res.json();
            const d = body?.data;
            if (d?.overall === 'degraded' || d?.overall === 'unavailable') {
                showToast(`外部依赖状态：${d.overall === 'unavailable' ? '部分服务暂不可用' : '部分服务状态异常'}，任务仍可尝试并会显示降级原因`, 'warning');
            }
        } catch (_) { /* 依赖状态不应阻断主界面 */ }
    }
    showDependencyHint();
    let agentStartedAt = 0;
    let agentStallTimer = null;

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
        const mark = step.status === 'done' ? '✓' : (step.status === 'running' ? '…' : '○');
        return `<li class="agent-step agent-step-${escapeHtml(step.status || 'todo')}"><span>${mark}</span><b>${escapeHtml(step.label || step.id)}</b>${step.message ? `<small>${escapeHtml(step.message)}</small>` : ''}</li>`;
    }

    function renderAgentPlan(observer, steps) {
        // 只展示已经发生的步骤；未来的固定 pending 芯片会制造“播放脚本”的错觉。
        const planSteps = (Array.isArray(observer?.plan_steps) ? observer.plan_steps : [])
            .filter(step => step && step.status && step.status !== 'pending');
        const actual = steps.filter(step => step && step.id).map(step => ({ id: step.id, label: step.label, status: step.status }));
        const merged = [...planSteps, ...actual].filter((step, i, all) => all.findIndex(x => x.id === step.id) === i);
        if (!merged.length) return '';
        return `<div class="agent-plan-strip">${merged.map(step => `
            <span class="agent-plan-chip agent-plan-${escapeHtml(step.status || 'pending')}">${escapeHtml(step.label || step.id)}</span>
        `).join('')}</div>`;
    }

    function agentEventNarrative(event) {
        const payload = event?.payload || {};
        const tool = payload.name || payload.failed_tool || '';
        if (event?.kind === 'model_decision') {
            if (payload.vision_used) {
                return {
                    title: 'GLM-5.3-Flash 视觉辅助判断',
                    body: [payload.summary || '已查看当前影像并生成公开决策摘要', payload.visual_observation ? `观察：${payload.visual_observation}` : '未提供独立视觉观察'].filter(Boolean).join(' · '),
                    why: (payload.why || []).join('；') || '结合影像、覆盖率和质量证据进行判断',
                };
            }
            return {
                title: '做出决策',
                body: payload.summary || (payload.tool_call ? `准备调用 ${payload.tool_call.name}` : '准备整理结论'),
                why: payload.tool_call ? `依据当前证据选择 ${payload.tool_call.name}` : '依据当前证据判断无需继续调用工具',
            };
        }
        if (event?.kind === 'model_unavailable') {
            return { title: '决策服务暂不可用', body: payload.summary || '模型决策服务未返回可用结果', why: (payload.why || []).join('；') || '可重试当前步骤，或明确选择规则流程' };
        }
        if (event?.kind === 'rule_decision') {
            return { title: '规则兜底决策', body: payload.summary || '按已确认规则继续执行', why: (payload.why || []).join('；') || '这不是模型自主决策' };
        }
        if (event?.kind === 'tool_started') {
            return { title: '开始执行', body: `调用 ${tool}`, why: payload.args ? `输入参数已锁定：${JSON.stringify(payload.args)}` : '' };
        }
        if (event?.kind === 'tool_result') {
            const result = payload.result || {};
            const failed = result.status === 'error' || result.available === false;
            const detail = result.message || result.reason || (result.result?.message) || (failed ? '工具未返回可用结果' : '已获得工具结果并写入证据链');
            return { title: failed ? '执行失败' : '完成并记录', body: `${tool}：${detail}`, why: failed ? '将根据失败类型决定重试、替代工具或请求用户' : '结果已进入后续决策上下文' };
        }
        if (event?.kind === 'replan_required') {
            return { title: '重新规划', body: payload.error || '上一步没有得到可用结果', why: payload.suggestion || '避免重复失败并选择可行的替代路径' };
        }
        if (event?.kind === 'checkpoint') return { title: '保存检查点', body: `已保存 ${payload.tool_count || 0} 次工具交换`, why: '支持断线恢复和 worker 接管' };
        if (event?.kind === 'plan_changed') return { title: '更新计划', body: payload.reason || '根据新证据调整执行计划', why: '让后续步骤与当前事实保持一致' };
        return { title: event?.kind || '执行事件', body: payload.message || payload.reason || '', why: '' };
    }

    function renderAgentEvent(event) {
        const n = agentEventNarrative(event);
        const status = event?.status || event?.payload?.status || 'running';
        const icon = status === 'done' ? '✓' : (status === 'failed' ? '!' : (status === 'warning' ? '⚠' : '•'));
        return `<li class="agent-event-item agent-event-${escapeHtml(status)}"><div><b><i>${icon}</i>${escapeHtml(n.title)}</b><small>#${escapeHtml(event?.sequence ?? '')} · ${escapeHtml(event?.created_at ? formatSceneDate(event.created_at) : '')}</small></div><p>${escapeHtml(n.body)}</p>${n.why ? `<em>为什么：${escapeHtml(n.why)}</em>` : ''}</li>`;
    }

    function renderAgentObserver(observer, steps, eventLog = []) {
        if (!observer || Object.keys(observer).length === 0) return '';
        const decision = observer.decision && typeof observer.decision === 'object' ? observer.decision : {};
        const why = Array.isArray(decision.why) ? decision.why : [];
        const latestDecision = [...eventLog].reverse().find(e => ['model_decision', 'rule_decision', 'model_unavailable'].includes(e?.kind));
        const decisionPayload = latestDecision?.payload || {};
        const decisionNarrative = latestDecision ? agentEventNarrative(latestDecision) : null;
        const decisionWhy = Array.isArray(decisionPayload.why) ? decisionPayload.why : why;
        const currentThought = decisionNarrative?.body || observer.public_thought || '';
        return `
            <div class="agent-observer">
                <div class="agent-observer-head">
                    <span>当前决策</span>
                    <b>${escapeHtml(observer.current_label || '准备中')}</b>
                </div>
                <div class="agent-observer-grid">
                    ${currentThought ? `<div><span>${escapeHtml(decisionNarrative?.title || '当前判断')}</span><p>${escapeHtml(currentThought)}</p></div>` : ''}
                    ${decisionWhy.length ? `<div><span>决策依据</span><p>${escapeHtml(decisionWhy.join('；'))}</p></div>` : ''}
                    ${observer.doing ? `<div><span>正在执行</span><p>${escapeHtml(observer.doing)}</p></div>` : ''}
                    ${observer.next ? `<div><span>下一步</span><p>${escapeHtml(observer.next)}</p></div>` : ''}
                </div>
                ${eventLog.length ? `<div class="agent-live-events"><span>专家执行记录</span><ol>${eventLog.slice(-8).map(renderAgentEvent).join('')}</ol></div>` : ''}
            </div>
        `;
    }

    function renderAgentSession(session) {
        if (!agentPanel || !session) return;
        agentPanel.hidden = false;
        const prevScrollTop = agentPanel.scrollTop;  // 轮询重绘后恢复滚动位置
        const prevImg = agentPanel.querySelector('.agent-result-img');  // 复用旧节点，URL 不变就不重新加载
        const artifacts = session.artifacts || {};
        const observer = session.observer || artifacts.observer || {};
        const ndwi = artifacts.ndwi;
        const finalAnswer = artifacts.final_answer || '';
        const report = artifacts.report;
        const waiting = artifacts.waiting;
        const steps = Array.isArray(session.timeline) ? session.timeline : [];
        const optionsHtml = waiting?.options?.length
            ? `<div class="agent-options">${waiting.options.map(opt => {
                const code = typeof opt === 'string' ? opt : (opt?.code || opt?.label || '');
                const label = typeof opt === 'string' ? opt : (opt?.label || opt?.code || '');
                return `<button data-agent-option="${escapeHtml(code)}" data-agent-label="${escapeHtml(label)}">${escapeHtml(label)}</button>`;
            }).join('')}</div>`
            : '';
        // 结果图不做 cache-bust：URL 不变就不重新请求（下方仅在 URL 变化时替换 src）
        const imageHtml = artifacts.image_url
            ? `<img class="agent-result-img" src="${escapeHtml(artifacts.image_url)}" alt="Agent 影像结果">`
            : '';
        const ndwiHtml = ndwi
            ? `<div class="agent-metric"><span>NDWI</span><b>${ndwi.available ? `${ndwi.water_percent}% 可能水体` : '未计算'}</b><p>${escapeHtml(ndwi.limitations || ndwi.reason || '')}</p></div>`
            : '';
        const cancelHtml = session.status === 'running'
            ? '<button id="agent-cancel-btn" class="agent-secondary-btn">取消任务</button>'
            : '';
        const scene = artifacts.scene || {};
        const sceneMeta = scene.metadata || {};
        const evidenceBits = [];
        if (scene.source_label || scene.source) evidenceBits.push(`来源 ${scene.source_label || scene.source}`);
        if (scene.decision_grade) evidenceBits.push(`证据级别 ${sceneGradeText(scene.decision_grade)}`);
        const originalGsd = sceneMeta.source_asset_gsd_m ?? scene.gsd_m;
        const previewScale = sceneMeta.preview_scale_m ?? sceneMeta.rendered_gsd_m;
        evidenceBits.push(originalGsd != null ? `原始 GSD ${Number(originalGsd).toFixed(1)}m/像素` : '原始 GSD 未知');
        if (previewScale != null) evidenceBits.push(`预览采样 ${Number(previewScale).toFixed(1)}m/像素`);
        evidenceBits.push(scene.cloud_percent != null ? `云量 ${Number(scene.cloud_percent).toFixed(1)}%` : '云量未知');
        evidenceBits.push(sceneMeta.target_coverage_ratio != null ? `覆盖 ${(Number(sceneMeta.target_coverage_ratio) * 100).toFixed(1)}%` : '覆盖未知');
        evidenceBits.push(sceneMeta.valid_image_ratio != null ? `有效像素 ${(Number(sceneMeta.valid_image_ratio) * 100).toFixed(1)}%` : '有效像素未知');
        if (sceneMeta.grid_shape) evidenceBits.push(`网格 ${sceneMeta.grid_shape.columns || '?'}×${sceneMeta.grid_shape.rows || '?'}`);
        if (ndwi?.sample_size_px != null) evidenceBits.push(`NDWI 样本 ${Number(ndwi.sample_size_px).toLocaleString('zh-CN')} px`);
        if (ndwi?.grid_failed_count != null && ndwi.grid_failed_count > 0) evidenceBits.push(`失败网格 ${ndwi.grid_failed_count}`);
        const evidenceHtml = evidenceBits.length
            ? `<div class="agent-metric agent-evidence-metric"><span>证据质量</span><b>${escapeHtml(evidenceBits.join(' · '))}</b><p>数字结论仅在上述有效区域和分辨率范围内成立；低质量影像不支持精确尺寸或水质参数。</p></div>`
            : '';
        const reportHtml = report
            ? `<a class="agent-report-link" href="${escapeHtml(report.download_url)}" target="_blank">下载 Word 报告</a>`
            : (session.status === 'completed' ? '<button id="agent-report-btn" class="agent-secondary-btn">生成报告</button>' : '');
        const transcriptHtml = session.status !== 'running'
            ? `<div class="agent-transcript-actions"><a href="/api/agent/sessions/${session.id}/transcript/?format=markdown" target="_blank">导出 Markdown</a><a href="/api/agent/sessions/${session.id}/transcript/?format=json" target="_blank">导出 JSON</a></div>`
            : '';
        const eventLog = (window.agentEventLog || []).slice(-12);
        const eventHtml = eventLog.length ? `<details class="agent-event-log"><summary>完整执行记录（${eventLog.length}）</summary><ol>${eventLog.map(renderAgentEvent).join('')}</ol></details>` : '';
        agentPanel.innerHTML = `
            <div class="agent-session-head">
                <span>${escapeHtml(agentStatusText(session.status))}</span>
                <b>#${session.id}</b>
                ${session.goal ? `<div class="agent-goal-echo" title="${escapeHtml(session.goal)}">目标：${escapeHtml(session.goal)}</div>` : ''}
            </div>
            ${renderAgentObserver(observer, steps, eventLog)}
            ${eventHtml}
            ${!eventLog.length ? `<ol class="agent-steps">${steps.map(agentStepText).join('')}</ol>` : ''}
            ${waiting ? `<div class="agent-waiting">${escapeHtml(waiting.message || '')}${optionsHtml}</div>` : ''}
            ${cancelHtml}
            ${imageHtml}
            ${evidenceHtml}
            ${ndwiHtml}
            ${finalAnswer ? `<div class="agent-final-answer">${renderMarkdown(finalAnswer)}</div>` : ''}
            ${reportHtml}
            ${transcriptHtml}
        `;
        // 仅当结果图 URL 变化时才让浏览器重新加载：URL 不变则换回旧 img 节点，避免轮询闪动
        const imgEl = agentPanel.querySelector('.agent-result-img');
        if (imgEl && prevImg && prevImg.getAttribute('src') === imgEl.getAttribute('src')) {
            imgEl.replaceWith(prevImg);
        }
        agentPanel.scrollTop = prevScrollTop;
        agentPanel.querySelectorAll('[data-agent-option]').forEach(btn => {
            btn.addEventListener('click', () => {
                const code = btn.dataset.agentOption || '';
                const label = btn.dataset.agentLabel || code || btn.textContent || '';
                sendAgentMessage(label, code);
            });
        });
        const reportBtn = document.getElementById('agent-report-btn');
        if (reportBtn) reportBtn.addEventListener('click', () => {
            if (reportBtn.disabled) return;  // 防连点
            reportBtn.disabled = true;
            sendAgentMessage('生成报告', 'generate_report');
        });
        const cancelBtn = document.getElementById('agent-cancel-btn');
        if (cancelBtn) cancelBtn.addEventListener('click', () => {
            if (cancelBtn.disabled) return;
            cancelBtn.disabled = true;
            sendAgentMessage('取消调查', 'cancel');
        });
        if (artifacts.scene) {
            const b = artifacts.bbox || artifacts.scene.bbox;
            if (b?.min_lat != null) fitMapBounds(dataBboxToMapBounds(b));
        }
    }

    async function loadAgentSession(id) {
        const res = await fetch(`/api/agent/sessions/${id}/`);
        if (res.status === 429) {
            // Agent 状态轮询被限流时，任务本身仍在后台运行；不能把 429 当成任务失败。
            return null;
        }
        const data = await res.json();
        if (data.code === 200) {
            // 事件流是执行事实；observer/timeline 只是兼容投影。
            try {
                const evRes = await fetch(`/api/agent/sessions/${id}/events/?after=${encodeURIComponent(window.agentEventCursor ?? -1)}`);
                if (evRes.ok) {
                    const evBody = await evRes.json();
                    const events = evBody?.data?.events || [];
                    window.agentEventLog = [...(window.agentEventLog || []), ...events].slice(-80);
                    if (evBody?.data?.next_cursor != null) window.agentEventCursor = evBody.data.next_cursor;
                }
            } catch (_) { /* 状态接口仍可独立工作 */ }
            renderAgentSession(data.data);
            if (data.data.status === 'completed' || data.data.status === 'failed' || data.data.status === 'waiting_user') {
                if (agentPollTimer) clearInterval(agentPollTimer);
                agentPollTimer = null;
                if (window.agentEventSource) {
                    window.agentEventSource.close();
                    window.agentEventSource = null;
                }
                agentRunBtn.disabled = false;
            }
            return data.data;
        }
        throw new Error(data.msg || 'Agent 状态读取失败');
    }

    function startAgentPolling(id) {
        if (agentPollTimer) clearInterval(agentPollTimer);
        window.agentEventCursor = -1;
        window.agentEventLog = [];
        window.agentEventReconnects = 0;
        window.agentStatusFailureCount = 0;
        agentStartedAt = Date.now();
        if (agentStallTimer) clearTimeout(agentStallTimer);
        agentStallTimer = setTimeout(() => {
            if (currentAgentSessionId === id && agentRunBtn?.disabled) {
                showToast('任务仍在后台处理中，进度面板会自动更新；如长时间无变化可刷新状态', 'info');
                const hint = agentPanel?.querySelector('.agent-boot-state small');
                if (hint) hint.textContent = '链路响应较慢，正在保留任务并等待后台阶段完成。';
            }
        }, 8000);
        agentPollTimer = setInterval(() => {
            loadAgentSession(id).catch(() => {
                window.agentStatusFailureCount = (window.agentStatusFailureCount || 0) + 1;
                if (window.agentStatusFailureCount >= 3) showToast('暂时无法获取最新状态，已保留当前任务内容', 'warning');
            });
        }, 3000);
    }

    function startAgentEventStream(id) {
        if (!window.EventSource) return;
        if (window.agentEventSource) window.agentEventSource.close();
        const source = new EventSource(`/api/agent/sessions/${id}/events/stream/?after=${encodeURIComponent(window.agentEventCursor ?? -1)}`);
        window.agentEventSource = source;
        source.addEventListener('agent_event', (event) => {
            try {
                const item = JSON.parse(event.data);
                window.agentEventLog = [...(window.agentEventLog || []), item].slice(-80);
                if (item.sequence != null) window.agentEventCursor = item.sequence;
                if (currentAgentSessionId === id) loadAgentSession(id).catch(() => {});
            } catch (_) { /* malformed event: polling remains available */ }
        });
        source.onerror = () => {
            source.close();
            window.agentEventSource = null;
            if (currentAgentSessionId === id) {
                showToast('实时连接暂时中断，已切换恢复轮询', 'info');
                // 让轮询先推进游标，再按退避重连 SSE；最多重连3次，避免网络异常时风暴。
                const attempt = Number(window.agentEventReconnects || 0) + 1;
                window.agentEventReconnects = attempt;
                if (attempt <= 3) {
                    window.setTimeout(() => {
                        if (currentAgentSessionId === id && !window.agentEventSource) startAgentEventStream(id);
                    }, Math.min(15000, 1000 * (2 ** (attempt - 1))));
                }
            }
        };
    }

    async function startAgentSession(extra = {}) {
        if (document.body.dataset.runEngine === 'dag') {
            if (!window.SatelliteRun) {
                showToast('调查控制模块未加载，请刷新页面后重试', 'error');
                return;
            }
            return window.SatelliteRun.start(extra);
        }
        const goal = (extra.goal || agentGoalInput?.value || '').trim();
        if (!goal) {
            showToast('请输入调查目标', 'warning');
            return;
        }
        agentRunBtn.disabled = true;
        showToast(agentModeSelect?.value === 'fast' ? '快速模式：预计 1 次视觉调用' : '精准模式：预计 2–4 次视觉调用，可能需要更长时间', 'info');
        const previousLabel = agentRunBtn.innerHTML;
        agentRunBtn.innerHTML = '<i class="ri-loader-4-line ri-spin" aria-hidden="true"></i>';
        agentRunBtn.setAttribute('aria-label', '正在启动调查');
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
            const requestId = extra.request_id || (window.crypto?.randomUUID ? window.crypto.randomUUID() : `agent-${Date.now()}-${Math.random().toString(16).slice(2)}`);
            const res = await fetch('/api/agent/sessions/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    goal,
                    mode: agentModeSelect?.value || 'precise',
                    request_id: requestId,
                    ...extra
                })
            });
            const data = await res.json();
            if (data.code !== 200) {
                agentRunBtn.disabled = false;
                agentRunBtn.innerHTML = previousLabel;
                agentRunBtn.setAttribute('aria-label', '启动调查');
                showToast(data.msg || 'Agent 启动失败', 'error');
                return;
            }
            currentAgentSessionId = data.data.id;
            renderAgentSession(data.data);
            startAgentPolling(currentAgentSessionId);
            startAgentEventStream(currentAgentSessionId);
            showToast('Agent 调查已启动', 'success');
        } catch (e) {
            agentRunBtn.disabled = false;
            agentRunBtn.innerHTML = previousLabel;
            agentRunBtn.setAttribute('aria-label', '启动调查');
            if (agentPanel) agentPanel.innerHTML += '<div class="agent-boot-state agent-error-state"><div><b>调查启动失败</b><small>请检查网络或服务状态后重试。</small></div></div>';
            showToast('Agent 请求失败，可重试', 'error');
        }
    }

    async function sendAgentMessage(content, action = '') {
        if (!currentAgentSessionId) return;
        try {
            const messageId = (window.crypto?.randomUUID ? window.crypto.randomUUID() : `msg-${Date.now()}-${Math.random().toString(16).slice(2)}`);
            const res = await fetch(`/api/agent/sessions/${currentAgentSessionId}/messages/`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content, action, message_id: messageId })
            });
            const data = await res.json();
            if (data.code === 200 || data.code === 202) {
                renderAgentSession(data.data);
                if (data.data?.status === 'running') {
                    agentRunBtn.disabled = true;
                    startAgentPolling(currentAgentSessionId);
                } else if (data.data?.status === 'completed' || data.data?.status === 'failed' || data.data?.status === 'waiting_user') {
                    agentRunBtn.disabled = false;
                }
                if (data.code === 202) {
                    showToast(data.msg || '请求正在处理中，请稍后刷新', 'info');
                    startAgentPolling(currentAgentSessionId);
                } else {
                    showToast(action === 'generate_report' ? '报告已生成' : '已发送给 Agent', 'success');
                }
            } else {
                agentRunBtn.disabled = false;
                agentRunBtn.innerHTML = '<i class="ri-play-line" aria-hidden="true"></i>';
                agentRunBtn.setAttribute('aria-label', '启动调查');
                if (agentStallTimer) { clearTimeout(agentStallTimer); agentStallTimer = null; }
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
    // 待机简报里的示例目标：填入输入框并聚焦，由用户确认后再启动（不自动消耗配额）
    document.getElementById('agent-empty')?.addEventListener('click', (e) => {
        const btn = e.target.closest('.agent-example');
        if (!btn || !agentGoalInput) return;
        agentGoalInput.value = btn.dataset.goal || btn.textContent.trim();
        agentGoalInput.focus();
        agentGoalInput.setSelectionRange(agentGoalInput.value.length, agentGoalInput.value.length);
    });

    // 全局快捷键：让高频入口始终可达，并避免浏览器默认行为抢占焦点。
    const placeSearchInput = document.querySelector('input[placeholder*="搜索地点"]');
    document.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
            e.preventDefault();
            placeSearchInput?.focus();
            placeSearchInput?.select();
        }
        if (e.key === 'Escape' && document.activeElement === placeSearchInput) {
            placeSearchInput.value = '';
            placeSearchInput.dispatchEvent(new Event('input', { bubbles: true }));
            placeSearchInput.blur();
        }
    });

    // ==========================================
    // 返回全国按钮
    // ==========================================
    const backBtn = document.getElementById('backButton');
    const zoomReadout = document.getElementById('map-zoom-readout');
    const gsdReadout = document.getElementById('map-gsd-readout');
    const cursorReadout = document.getElementById('map-cursor-readout');
    // 光标经纬度读数：方位后缀跟随半球，便于直接读方位
    function formatCursorLatLng(latlng) {
        const lat = Math.abs(latlng.lat).toFixed(3);
        const lng = Math.abs(latlng.lng).toFixed(3);
        return `${lat}°${latlng.lat >= 0 ? 'N' : 'S'} ${lng}°${latlng.lng >= 0 ? 'E' : 'W'}`;
    }
    // Web 墨卡托地面分辨率实时估算：156543.03392 * cos(lat) / 2^zoom (m/px)
    function formatGroundResolution() {
        const metersPerPx = 156543.03392 * Math.cos(map.getCenter().lat * Math.PI / 180) / Math.pow(2, map.getZoom());
        if (metersPerPx >= 1000) return `${(metersPerPx / 1000).toFixed(2)} km/px`;
        if (metersPerPx >= 10) return `${Math.round(metersPerPx)} m/px`;
        return `${metersPerPx.toFixed(2)} m/px`;
    }
    function updateBackBtn() {
        backBtn.classList.toggle('is-visible', map.getZoom() >= 7);
        if (zoomReadout) zoomReadout.textContent = map.getZoom();
        if (gsdReadout) gsdReadout.textContent = formatGroundResolution();
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

    const compareBarHint = document.getElementById('compare-bar-hint');

    compareToggle.addEventListener('click', () => {
        compareMode = !compareMode;
        compareToggle.classList.toggle('is-active', compareMode);
        document.body.classList.toggle('compare-mode', compareMode);
        if (!compareMode) {
            document.querySelectorAll('.select-cb').forEach(cb => cb.checked = false);
            document.querySelectorAll('.coord-item.is-selected').forEach(el => el.classList.remove('is-selected'));
        }
        updateCompareBar();
    });

    // 对比条在对比模式下常显：不足 2 个勾选时禁用按钮并说明还差几个
    function updateCompareBar() {
        const checked = document.querySelectorAll('.select-cb:checked').length;
        compareBar.hidden = !compareMode;
        compareBtn.disabled = checked < 2;
        compareBtn.textContent = `对比所选 (${checked} 个区域)`;
        if (compareBarHint) {
            compareBarHint.textContent = checked < 2 ? `再勾选 ${2 - checked} 个区域即可开始对比` : '';
        }
    }
    document.addEventListener('change', (e) => {
        if (e.target.classList.contains('select-cb')) {
            // 勾选同步卡片高亮
            const item = e.target.closest('.coord-item');
            if (item) item.classList.toggle('is-selected', e.target.checked);
            updateCompareBar();
        }
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
        container.style.display = 'flex';   // 左栏默认 display:block，flex 布局需显式开启
        container.style.flexDirection = 'row';
        container.style.flexWrap = 'wrap';
        container.style.gap = '8px';
        container.style.alignContent = 'flex-start';
        items.forEach((item, i) => {
            const figure = document.createElement('figure');
            figure.className = 'compare-figure';
            const img = document.createElement('img');
            img.src = item.imgUrl;
            img.className = 'compare-preview-img';
            img.alt = `区域 ${i + 1}`;
            const cap = document.createElement('figcaption');
            cap.className = 'compare-figure-cap';
            cap.textContent = `区域 #${i + 1}`;
            figure.appendChild(img);
            figure.appendChild(cap);
            container.appendChild(figure);
        });

        chatMemories['__compare__'] = {
            history: [],
            spatial: '',
            compareFiles: items.map(x => x.fileName)
        };
        renderChatHistory();
        showChatModal();
        updatePromptScene();
        setTimeout(() => textarea.focus(), 100);
    }

    // ==========================================
    // 地图提示自动隐藏
    // ==========================================
    const mapHint = document.getElementById('map-hint');
    let hintHidden = false;
    // 触屏没有右键：换成「再次长按取消」（touchstart 里再次长按会清除旧框选）
    if (mapHint && window.matchMedia('(pointer: coarse)').matches) {
        mapHint.textContent = '长按拖拽框选区域 · 再次长按取消';
    }
    if (mapHint) requestAnimationFrame(() => mapHint.classList.add('visible'));
    function hideHint() {
        if (!hintHidden && mapHint) {
            hintHidden = true;
            mapHint.classList.remove('visible');
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
                style: { color: "#E4B36A", weight: 0.9, opacity: 0.45, fillOpacity: 0, fillColor: "transparent" },
                onEachFeature: (feature, layer) => {
                    layer.on({
                        mouseover: (e) => {
                            isMouseOverChina = true;
                            e.target.setStyle({ color: "#F2CD90", weight: 2, fillOpacity: 0.08, fillColor: "#E4B36A" });
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
        if (cursorReadout) cursorReadout.textContent = formatCursorLatLng(e.latlng);
        if (!isSelecting) return;
        const bounds = L.latLngBounds(startLatLng, e.latlng);
        if (!selectionRect) {
            selectionRect = L.rectangle(bounds, { color: "#E4B36A", weight: 3, fillOpacity: 0.12 }).addTo(map);
        } else {
            selectionRect.setBounds(bounds);
        }
    });

    map.on('mouseout', () => {
        if (cursorReadout) cursorReadout.textContent = '—';
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
            selectionRect = L.rectangle(bounds, { color: "#E4B36A", weight: 3, fillOpacity: 0.12 }).addTo(map);
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
            item._mapRect = selectionRect;  // 与鼠标 mouseup 分支一致：删除卡片时同步移除地图金框
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
        const sourceCfg = imagerySourceConfig(imagerySource) || IMAGERY_SOURCE_CONFIG.mapbox;
        const endpoint = sourceCfg.endpoint;

        try {
            setRegionStage(itemEl, 'pending', '请求影像中');
            status.style.display = 'block';
            status.innerHTML = `<span class="spinner"></span> 正在获取${imagerySourceLabel(imagerySource)}...`;

            const r = await fetch(endpoint, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ min_lng, max_lng, min_lat, max_lat, ...(sourceCfg.collection ? { collection: sourceCfg.collection } : {}), ...(sourceCfg.basemapSource ? { basemap_source: sourceCfg.basemapSource } : {}) })
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

                if (sourceCfg.skipTileProgress) {
                    progressBar.style.display = 'none';
                    progressBar.hidden = true;
                    showReadyImage();
                    showToast(`${imagerySourceLabel(imagerySource)}已生成`, 'success');
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
                status.textContent = '抓取失败：' + d.msg;
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

    // 模态开合：backdrop 淡入淡出，关闭动画结束后再 display:none；焦点归还触发元素
    let modalLastFocus = null;
    const isChatModalOpen = () => modal.style.display === 'flex';

    function showChatModal() {
        if (!isChatModalOpen()) modalLastFocus = document.activeElement;
        modal.style.display = 'flex';
        requestAnimationFrame(() => {
            modal.classList.add('is-open');
            // 模态从 display:none 展开时 renderChatHistory 里的滚底拿不到布局，须在此重滚
            chatBox.scrollTop = chatBox.scrollHeight;
        });
    }

    function closeChatModal() {
        if (!isChatModalOpen()) return;
        modal.classList.remove('is-open');
        setTimeout(() => {
            if (!modal.classList.contains('is-open')) modal.style.display = 'none';
        }, 340);
        if (modalLastFocus && document.contains(modalLastFocus)) {
            try { modalLastFocus.focus(); } catch (err) {}
        }
        modalLastFocus = null;
    }

    // Tab 焦点循环：模态打开期间焦点不离开分析舱
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Tab' || !isChatModalOpen()) return;
        const focusables = Array.from(modal.querySelectorAll('button, [href], input, select, textarea, [tabindex]'))
            .filter(el => !el.disabled && el.tabIndex >= 0 && el.getClientRects().length > 0);
        if (!focusables.length) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        const active = document.activeElement;
        if (e.shiftKey) {
            if (active === first || !modal.contains(active)) { e.preventDefault(); last.focus(); }
        } else if (active === last || !modal.contains(active)) {
            e.preventDefault(); first.focus();
        }
    });

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

        const sourceGsd = Number(scene.metadata?.source_asset_gsd_m || 0);
        const renderedGsd = Number(scene.metadata?.rendered_gsd_m || scene.gsd_m || 0);
        const sceneCfg = imagerySourceConfig(scene.source);
        const isSentinel = !!(sceneCfg && sceneCfg.collection) || String(scene.source_label || '').includes('Sentinel-2');
        const gsd = isSentinel && sourceGsd > 0 && renderedGsd > 0
            ? `原始 ${sourceGsd}m · 预览采样约 ${renderedGsd}m/像素`
            : renderedGsd > 0 ? `约 ${renderedGsd} m/像素` : '未知';
        const cloud = scene.cloud_percent != null
            ? `${scene.cloud_percent}%`
            : (scene.source === 'sentinel1' ? '不受云影响' : '未知');
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
        let sourceChip = '';
        if (scene.source === 'sentinel1') {
            const instruments = scene.metadata?.instruments || scene.source_label || 'SAR · 全天候';
            sourceChip = `<span title="极化 / 合成孔径雷达"><i class="ri-radar-line" aria-hidden="true"></i>${escapeHtml(String(instruments))}</span>`;
        } else if (scene.source === 'copdem') {
            sourceChip = `<span title="静态 DEM"><i class="ri-mountain-line" aria-hidden="true"></i>静态 DEM · 采集基线 2011-2015</span>`;
        } else if (scene.source === 'tianditu' || scene.source === 'esri') {
            const basemapLabel = scene.source === 'tianditu' ? '天地图影像' : 'Esri World Imagery';
            sourceChip = `<span title="高清底图，无拍摄时间，仅视觉参考"><i class="ri-map-2-line" aria-hidden="true"></i>${basemapLabel} · 无拍摄时间</span>`;
        }
        const cloudChip = scene.source === 'copdem' ? '' : `<span title="云量"><i class="ri-cloudy-line" aria-hidden="true"></i>${escapeHtml(cloud)}</span>`;
        panel.innerHTML = `
            <div class="scene-compact-head">
                <span>${escapeHtml(scene.source_label || scene.source || '影像源')}</span>
                <strong title="${escapeHtml(limitations)}">${escapeHtml(grade)}</strong>
            </div>
            <div class="scene-chip-row">
                <span title="拍摄日期"><i class="ri-calendar-line" aria-hidden="true"></i>${escapeHtml(acquiredAt)}</span>
                <span title="原始影像分辨率与当前预览采样尺度"><i class="ri-ruler-line" aria-hidden="true"></i>${escapeHtml(gsd)}</span>
                ${cloudChip}
                ${sourceChip}
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
        showChatModal();
        updatePromptScene();
        setTimeout(() => textarea.focus(), 100);
    }

    closeBtn.onclick = () => { closeChatModal(); };

    // 影像预览点击在新标签打开原图（modalImg 会被 restoreChatModalLayout 重建，用代理）
    document.querySelector('.chat-modal-left').addEventListener('click', (e) => {
        if (e.target.id === 'chat-modal-img' && e.target.src) {
            window.open(e.target.src, '_blank', 'noopener');
        }
    });

    // 点击背景关闭
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closeChatModal();
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
        btn.classList.toggle('active', isOpen);
        btn.setAttribute('aria-expanded', String(isOpen));
        if (isOpen) updatePromptScene();
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
        const toggleBtn = document.getElementById('chat-prompt-toggle');
        toggleBtn.classList.remove('active');
        toggleBtn.setAttribute('aria-expanded', 'false');
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

        // 块级代码 ```：最先处理并占位，避免内部内容被后续行内规则/表格/换行污染
        const preBlocks = [];
        html = html.replace(/```[^\n]*\n([\s\S]*?)```/g, (m, code) => {
            preBlocks.push(code.replace(/\n$/, ''));
            return `\u0000PRE${preBlocks.length - 1}\u0000`;
        });

        // 表格处理（先做，内部单元格再补内联格式化）
        html = html.replace(/(\|[^\n]+\|\n\|[-:|\s]+\|\n(?:\|[^\n]+\|\n?)*)/gm, (match) => {
            const rows = match.trim().split('\n');
            let tableHtml = '<div class="md-table-wrap"><table class="md-table">';
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
            tableHtml += '</table></div>';
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
                } else if (!line.startsWith('\u0000') && !line.startsWith('<table') && !line.startsWith('<tr') && !line.startsWith('<td') && !line.startsWith('<th') && !line.startsWith('</table') && !line.startsWith('</tr') && !line.startsWith('</td') && !line.startsWith('</th') && !line.startsWith('<div') && !line.startsWith('<ul') && !line.startsWith('<ol') && !line.startsWith('<li') && !line.startsWith('</ul') && !line.startsWith('</ol') && !line.startsWith('</div') && !line.startsWith('<strong') && !line.startsWith('<em') && !line.startsWith('<br') && !line.startsWith('<code')) {
                    result.push(`<p>${line}</p>`);
                } else {
                    result.push(line);
                }
            }
        }
        if (inUl) result.push('</ul>');
        if (inOl) result.push('</ol>');
        // 还原块级代码占位符（内容已转义，直接输出 pre.md-pre>code）
        return result.join('\n').replace(/\u0000PRE(\d+)\u0000/g, (m, i) =>
            `<pre class="md-pre"><code>${preBlocks[Number(i)]}</code></pre>`);
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
        left.style.display = '';
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
            let popup = `<b>${escapeHtml(t.label || 'AI 定位目标')}</b>`;
            if (t.width_m != null) popup += `<br>尺寸约 ${escapeHtml(String(t.width_m))}m × ${escapeHtml(String(t.height_m))}m`;
            if (t.area_m2 != null) popup += `<br>占地约 ${t.area_m2 >= 10000 ? escapeHtml((t.area_m2 / 10000).toFixed(2)) + ' 公顷' : escapeHtml(String(Math.round(t.area_m2))) + ' m²'}`;
            popup += `<br>${escapeHtml(String(t.lat))}°N, ${escapeHtml(String(t.lng))}°E`;
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
        loadingDiv.innerHTML = `<span class="chat-typing"><span class="chat-typing-dots"><i></i><i></i><i></i></span> SatelliteSense ${analysisMode.label}${isCompare ? '正在对比分析' : '正在分析'}...</span>`;
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
        if (currentActiveImage === '__compare__' || !currentActiveImage) {
            showToast('请先打开一个区域的影像，再使用放大', 'warning');
            return;
        }
        zoomMode = !zoomMode;
        zoomBtn.classList.toggle('is-active', zoomMode);
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
        zoomBtn.classList.remove('is-active');  // 收尾交给 class，内联样式会压住 CSS 态
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
    const searchBox = document.getElementById('search-box');
    let searchTimeout = null;
    let searchIdx = -1;

    // ≤960px 顶栏 overflow-x:auto 会裁剪绝对定位的搜索结果：
    // 窄屏下移到 body 改 position:fixed，按输入框视口坐标定位（.glass 的 backdrop-filter 会劫持 fixed）
    function portalSearchResults() {
        if (narrowLayout() && searchResults.style.display === 'block' && !searchResults.classList.contains('is-portaled')) {
            const r = searchInput.getBoundingClientRect();
            document.body.appendChild(searchResults);
            searchResults.classList.add('is-portaled');
            searchResults.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 316)) + 'px';
            searchResults.style.top = (r.bottom + 8) + 'px';
        }
    }
    function restoreSearchResults() {
        if (searchResults.classList.contains('is-portaled')) {
            searchResults.classList.remove('is-portaled');
            searchResults.style.left = '';
            searchResults.style.top = '';
            searchBox.appendChild(searchResults);
        }
    }

    searchInput.addEventListener('input', () => {
        clearTimeout(searchTimeout);
        const q = searchInput.value.trim();
        if (q.length === 0) { searchResults.style.display = 'none'; restoreSearchResults(); searchIdx = -1; return; }
        searchResults.style.display = 'block';
        portalSearchResults();
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
                        div.innerHTML = `<div class="name">${escapeHtml(name)}</div><div class="detail">${escapeHtml(item.display_name)}</div>`;
                        div.addEventListener('mousedown', (e) => {
                            e.preventDefault();
                            map.flyTo([item.lat, item.lon], 14, { duration: 1.2 });
                            searchInput.value = name;
                            searchResults.style.display = 'none';
                            restoreSearchResults();
                            searchIdx = -1;
                            showToast('已定位: ' + name, 'info');
                        });
                        searchResults.appendChild(div);
                    });
                }
                searchResults.style.display = 'block';
                portalSearchResults();
            } catch(e) {
                searchResults.innerHTML = '<div class="search-result-item search-state">搜索服务不可用</div>';
                searchResults.style.display = 'block';
                portalSearchResults();
            }
        }, 250);
    });

    searchInput.addEventListener('keydown', (e) => {
        const items = searchResults.querySelectorAll('.search-result-item');
        if (e.key === 'Escape') { searchResults.style.display = 'none'; restoreSearchResults(); searchIdx = -1; searchInput.blur(); return; }
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
        items.forEach((item, i) => item.classList.toggle('is-active', i === searchIdx));
    });

    document.addEventListener('mousedown', (e) => {
        if (!e.target.closest('#search-box') && !e.target.closest('#search-results')) { searchResults.style.display = 'none'; restoreSearchResults(); searchIdx = -1; }
    });

    searchInput.addEventListener('focus', () => {
        if (searchInput.value.trim().length > 0) { searchResults.style.display = 'block'; portalSearchResults(); }
    });

    // 视口尺寸变化时重算 portal 位置（或收回顶栏）
    window.addEventListener('resize', () => {
        if (searchResults.classList.contains('is-portaled')) {
            searchResults.classList.remove('is-portaled');
            searchResults.style.left = '';
            searchResults.style.top = '';
            searchBox.appendChild(searchResults);
            if (searchResults.style.display === 'block' && narrowLayout()) portalSearchResults();
        }
    });

    // ==========================================
    // 图层切换
    // ==========================================
    const adminToggle = document.getElementById('layer-admin');
    const roadsToggle = document.getElementById('layer-roads');
    const spectralIndexList = document.getElementById('spectral-index-list');
    async function loadSpectralIndexCatalog() {
        if (!spectralIndexList) return;
        try {
            const response = await fetch('/api/analysis/indices/');
            const payload = await response.json();
            const indices = payload?.data?.indices || {};
            spectralIndexList.textContent = '';
            Object.entries(indices).forEach(([key, meta]) => {
                const item = document.createElement('span');
                item.className = 'spectral-chip';
                item.setAttribute('role', 'listitem');
                item.title = meta.implemented
                    ? `${meta.label || key} · 波段 ${(meta.bands || []).join(' / ')} · 已接通`
                    : `${meta.label || key} · 当前仅为指标目录，尚未接通真实波段执行链`;
                item.textContent = meta.implemented ? key.toUpperCase() : `${key.toUpperCase()} · 目录`;
                item.dataset.implemented = meta.implemented ? 'true' : 'false';
                item.setAttribute('aria-disabled', meta.implemented ? 'false' : 'true');
                item.classList.toggle('is-catalog-only', !meta.implemented);
                spectralIndexList.appendChild(item);
            });
        } catch (error) {
            spectralIndexList.textContent = '指标目录暂不可用';
        }
    }
    loadSpectralIndexCatalog();
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
        // Esc 优先于输入框 early-return：textarea 聚焦时也要能关模态/取消框选
        if (e.key === 'Escape') {
            let cancelledSel = false;
            if (selectionRect) { map.removeLayer(selectionRect); selectionRect = null; cancelledSel = true; }
            if (isSelecting) { isSelecting = false; map.dragging.enable(); cancelledSel = true; }
            if (isChatModalOpen()) closeChatModal();
            // 只提示真正取消了的"框选";不谎称能中断已发起的下载/AI 请求
            if (cancelledSel) showToast('已取消框选', 'info');
            return;
        }
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') {
            if (e.key === 'Enter' && e.ctrlKey) {
                e.preventDefault();
                sendBtn.click();
            }
            return;
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
                item.tabIndex = 0;
                item.setAttribute('role', 'button');
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
                        showChatModal();
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
                // 键盘可达：Enter/Space 触发与点击相同的加载
                item.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        item.click();
                    }
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
    const reportBtn = document.getElementById('report-btn');
    reportBtn.addEventListener('click', async () => {
        if (!currentActiveImage) {
            showToast('请先打开一个区域的影像，再生成报告', 'warning');
            return;
        }
        if (currentActiveImage === '__compare__') {
            showToast('对比模式暂不支持导出报告，请打开单个区域', 'warning');
            return;
        }
        if (reportBtn.disabled) return;   // 防连点
        reportBtn.disabled = true;
        // 忙碌态：spinner + 文案，结束恢复
        const btnHtml = reportBtn.innerHTML;
        reportBtn.innerHTML = '<span class="spinner"></span>生成中';
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
                // 用隐藏 a[download] 触发下载：直接改 location.href 失败时会整页跳走
                const a = document.createElement('a');
                a.href = d.data.download_url;
                a.download = '';
                a.style.display = 'none';
                document.body.appendChild(a);
                a.click();
                a.remove();
                showToast('报告已生成，正在下载', 'success');
            } else if (d.code === 410) {
                showToast(d.msg || '卫星图文件已丢失，请重新框选', 'error');
                loadHistories();
            } else {
                showToast('报告生成失败：' + d.msg, 'error');
            }
        } catch (e) {
            showToast('报告生成失败', 'error');
        } finally {
            reportBtn.disabled = false;
            reportBtn.innerHTML = btnHtml;
        }
    });

    // 聊天发送后自动保存（sendBtn.onclick 里 AI 回复后调用）

    loadHistories();

    initMapLayers();
});
