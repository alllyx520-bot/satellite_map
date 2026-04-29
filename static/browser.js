document.addEventListener('DOMContentLoaded', () => {
    const normalMap = L.tileLayer('https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}', {
        subdomains: ["1", "2", "3", "4"],
        attribution: '&copy; 高德地图'
    });

    const satelliteMap = L.tileLayer('https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}', {
        subdomains: ["1", "2", "3", "4"],
        attribution: '&copy; 高德地图(卫星)'
    });

    const INITIAL_VIEW = { center: [36.0, 105.0], zoom: 4 };
    const map = L.map('map', {
        minZoom: 3, maxZoom: 18,
        maxBounds: [[-10, 70], [65, 140]],
        layers: [normalMap]
    }).setView(INITIAL_VIEW.center, INITIAL_VIEW.zoom);

    const styleToggle = document.getElementById('map-style-toggle');
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

    // ==========================================
    // 返回全国按钮
    // ==========================================
    const backBtn = document.getElementById('backButton');
    function updateBackBtn() {
        backBtn.style.display = map.getZoom() >= 7 ? 'block' : 'none';
    }
    map.on('zoomend', updateBackBtn);
    map.on('moveend', updateBackBtn);
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

    compareToggle.addEventListener('click', () => {
        compareMode = !compareMode;
        compareToggle.style.color = compareMode ? '#007aff' : 'rgba(255,255,255,0.4)';
        document.querySelectorAll('.select-cb').forEach(cb => cb.style.display = compareMode ? 'block' : 'none');
        if (!compareMode) {
            document.querySelectorAll('.select-cb').forEach(cb => cb.checked = false);
            compareBar.style.display = 'none';
        }
    });

    function updateCompareBar() {
        const checked = document.querySelectorAll('.select-cb:checked').length;
        compareBar.style.display = compareMode && checked >= 2 ? 'block' : 'none';
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
        modalImg.style.display = 'none';
        modalIdSpan.innerText = ` · 对比 ${items.length} 个区域`;
        chatBox.innerHTML = `<div style="color:#aaa;text-align:center;margin:20px 0;">加载 ${items.length} 张影像...</div>`;

        const container = document.querySelector('.chat-modal-left');
        container.innerHTML = '';
        container.style.position = '';
        container.style.flexWrap = 'wrap';
        container.style.gap = '8px';
        container.style.alignContent = 'flex-start';
        items.forEach((item, i) => {
            const img = document.createElement('img');
            img.src = item.imgUrl;
            img.style.width = 'calc(50% - 4px)';
            img.style.borderRadius = '8px';
            img.style.border = '0.5px solid rgba(255,255,255,0.1)';
            img.style.objectFit = 'cover';
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
                color: 'none', fillColor: '#f0f0f0', fillOpacity: 1, interactive: false, renderer: L.canvas()
            }).addTo(map);

            provinceLayer = L.geoJSON(geojson, {
                style: { color: "#888", weight: 1, fillOpacity: 0, fillColor: "transparent" },
                onEachFeature: (feature, layer) => {
                    layer.on({
                        mouseover: (e) => {
                            isMouseOverChina = true;
                            e.target.setStyle({ color: "#ff4757", weight: 3, fillOpacity: 0.1, fillColor: "#ff4757" });
                            e.target.bringToFront();
                        },
                        mouseout: (e) => {
                            isMouseOverChina = false;
                            provinceLayer.resetStyle(e.target);
                        },
                        click: (e) => { if (!isSelecting && !justFinished) map.fitBounds(e.target.getBounds()); }
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
        const div = document.createElement('div');
        div.className = 'coord-item';
        div.style.position = 'relative';
        div.innerHTML = `
            <button class="delete-btn" title="删除此项">✕</button>
            <input type="checkbox" class="select-cb" style="position:absolute;top:8px;left:8px;width:16px;height:16px;accent-color:#007aff;cursor:pointer;display:none;">
            <strong><span style="color:#3b82f6">●</span> 区域 #${c}</strong><br>
            <span style="color:#aaa">${nw.lat.toFixed(4)}, ${nw.lng.toFixed(4)}  →  ${se.lat.toFixed(4)}, ${se.lng.toFixed(4)}</span>
            <img class="preview-img" alt="卫星图预览" style="cursor:pointer;" title="点击进入分析舱">
            <div class="tile-progress" style="display:none; margin-top:8px;">
                <div class="progress-bar-bg" style="width:100%; height:4px; background:rgba(255,255,255,0.08); border-radius:2px; overflow:hidden;">
                    <div class="progress-bar-fill" style="width:0%; height:100%; background:linear-gradient(90deg, #007aff, #34c759); border-radius:2px; transition:width 0.3s;"></div>
                </div>
                <span class="progress-text" style="font-size:11px; color:rgba(255,255,255,0.4);">0/0</span>
            </div>
            <div class="ai-status">
                <span class="spinner"></span> 正在抓取高清卫星图...
            </div>
            <div class="ai-question" style="margin-top:10px; display:none;">
                <button class="enter-cabin-btn" style="width:100%; padding:10px; background:#007aff; color:white; border:none; border-radius:9999px; cursor:pointer; font-weight:600; font-size:13px; transition:all 0.3s cubic-bezier(0.25,0.1,0.25,1);">
                    进入分析舱
                </button>
            </div>
        `;

        // 删除按钮
        const delBtn = div.querySelector('.delete-btn');
        delBtn.addEventListener('click', (e) => {
            e.stopPropagation();
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
                if (strong) strong.innerHTML = `<span style="color:#3b82f6">●</span> 区域 #${i + 1}`;
            });
            showToast('已删除该区域', 'info');
        });

        // 点击预览放大（跳转到当前区域 + 打开模态）
        const previewImg = div.querySelector('.preview-img');
        previewImg.addEventListener('click', function() {
            if (this.dataset.filename) {
                map.fitBounds([[se.lat, nw.lng], [nw.lat, se.lng]]);
                openChatModal(this.dataset.filename, this.src);
            }
        });

        list.prepend(div);
        updateSidebarUI();
        return div;
    }

    async function sendToBackend(nw, se, itemEl) {
        const min_lng = Math.min(nw.lng, se.lng);
        const max_lng = Math.max(nw.lng, se.lng);
        const min_lat = Math.min(nw.lat, se.lat);
        const max_lat = Math.max(nw.lat, se.lat);

        const status = itemEl.querySelector('.ai-status');
        const previewImg = itemEl.querySelector('.preview-img');
        const questionBox = itemEl.querySelector('.ai-question');
        const enterBtn = itemEl.querySelector('.enter-cabin-btn');
        const progressBar = itemEl.querySelector('.tile-progress');
        const fill = progressBar.querySelector('.progress-bar-fill');
        const progressText = progressBar.querySelector('.progress-text');
        let pollTimer = null;

        try {
            const r = await fetch("http://127.0.0.1:8000/api/satellite/get-img/", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ min_lng, max_lng, min_lat, max_lat })
            });
            const d = await r.json();

            if (d.code === 200) {
                const fileName = d.data.file_name;
                const totalTiles = d.data.total_tiles || 1;
                const imgUrl = `http://127.0.0.1:8000/api/satellite/show-img/?file=${fileName}`;
                const spatialCtx = d.data.gsd_m
                    ? `范围: ${d.data.area_km2} km\u00B2 | 分辨率: ${d.data.gsd_m} m/像素`
                    : "";

                chatMemories[fileName] = { history: [], spatial: spatialCtx, bbox: { min_lng, max_lng, min_lat, max_lat } };

                status.style.display = 'block';
                status.innerHTML = '<span class="spinner"></span> 正在下载瓦片...';
                progressBar.style.display = 'block';
                progressText.textContent = `0/${totalTiles}`;

                pollTimer = setInterval(async () => {
                    try {
                        const pr = await fetch(`http://127.0.0.1:8000/api/satellite/progress/?file=${fileName}`);
                        const pd = await pr.json();
                        if (pd.code === 200 && pd.data) {
                            const done = pd.data.done || 0;
                            const pct = Math.round((done / totalTiles) * 100);
                            fill.style.width = pct + '%';
                            progressText.textContent = `${done}/${totalTiles}`;
                            if (pd.data.status === 'done') {
                                clearInterval(pollTimer);
                                progressBar.style.display = 'none';
                                status.style.display = 'none';
                                previewImg.src = imgUrl + '&t=' + Date.now();
                                previewImg.dataset.filename = fileName;
                                previewImg.style.display = 'block';
                                previewImg.style.animation = 'none';
                                void previewImg.offsetHeight;
                                previewImg.style.animation = 'fadeIn 0.4s ease';
                                questionBox.style.display = 'block';
                                enterBtn.onclick = () => {
                                    map.fitBounds([[se.lat, nw.lng], [nw.lat, se.lng]]);
                                    openChatModal(fileName, imgUrl, spatialCtx);
                                };
                                showToast('卫星图抓取成功', 'success');
                            } else if (pd.data.status === 'error') {
                                clearInterval(pollTimer);
                                progressBar.style.display = 'none';
                                status.innerHTML = '抓取失败，请重试';
                                showToast('抓取失败', 'error');
                            }
                        }
                    } catch (e) {}
                }, 500);
            } else {
                status.innerHTML = '抓取失败：' + d.msg;
                showToast('抓取失败：' + d.msg, 'error');
            }
        } catch (e) {
            if (pollTimer) clearInterval(pollTimer);
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

    function openChatModal(fileName, imgUrl, spatialCtx) {
        currentActiveImage = fileName;
        currentSpatialCtx = spatialCtx || "";
        modalImg.src = imgUrl;
        modalIdSpan.innerText = currentSpatialCtx ? ` · ${currentSpatialCtx}` : '';
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

    function renderChatHistory() {
        chatBox.innerHTML = '';
        const data = getChatData(currentActiveImage);
        const history = data.history || [];
        if (history.length === 0) {
            chatBox.innerHTML = '<div style="color:#aaa; text-align:center; margin-top:50px;">🛰️ 可以开始提问了</div>';
        } else {
            history.forEach(msg => {
                const div = document.createElement('div');
                div.className = `chat-bubble ${msg.role === 'user' ? 'chat-user' : 'chat-ai'}`;
                if (msg.role === 'ai') {
                    div.innerHTML = renderMarkdown(msg.content);
                } else {
                    div.textContent = msg.content;
                }
                chatBox.appendChild(div);
            });
        }
        chatBox.scrollTop = chatBox.scrollHeight;
    }

    function restoreChatModalLayout() {
        const left = document.querySelector('.chat-modal-left');
        left.innerHTML = '<img id="chat-modal-img" src="" alt="卫星图放大版" draggable="false">';
        left.style.flexWrap = '';
        left.style.gap = '';
        left.style.alignContent = '';
        // 重新捕获 modalImg 引用
        modalImg = document.getElementById('chat-modal-img');
        modalImg.src = currentActiveImage && currentActiveImage !== '__compare__'
            ? `http://127.0.0.1:8000/api/satellite/show-img/?file=${currentActiveImage}`
            : '';
        modalImg.style.display = '';
    }

    sendBtn.onclick = async () => {
        const text = textarea.value.trim();
        if (!text) return;

        const data = getChatData(currentActiveImage);
        data.history.push({ role: 'user', content: text });
        textarea.value = '';
        autoResize();
        renderChatHistory();

        const isCompare = currentActiveImage === '__compare__';
        const currentModel = document.getElementById('model-select').value || 'qwen3.5-plus';
        const loadingDiv = document.createElement('div');
        loadingDiv.className = 'chat-bubble chat-ai';
        loadingDiv.innerHTML = `<span class="spinner"></span> SatelliteSense ${isCompare ? '正在对比分析' : '正在分析'}...`;
        chatBox.appendChild(loadingDiv);
        chatBox.scrollTop = chatBox.scrollHeight;

        try {
            const body = isCompare
                ? { file_names: data.compareFiles, question: text, history: data.history.slice(0, -1), model: currentModel }
                : { file_name: currentActiveImage, question: text, history: data.history.slice(0, -1), spatial_context: currentSpatialCtx, model: currentModel };
            const res = await fetch("http://127.0.0.1:8000/api/ai/query-region/", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body)
            });
            const result = await res.json();

            loadingDiv.remove();

            if (result.code === 200) {
                data.history.push({ role: 'ai', content: result.data.answer });
            } else {
                data.history.push({ role: 'ai', content: "❌ 分析失败: " + result.msg });
                showToast('AI 分析失败', 'error');
            }
        } catch(e) {
            loadingDiv.remove();
            data.history.push({ role: 'ai', content: "⚠️ 网络请求异常，请检查后端状态" });
            showToast('网络请求异常', 'error');
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
        zoomBtn.textContent = zoomMode ? '✕ 退出' : '🔍 放大';

        if (zoomMode) {
            const container = ensureZoomContainer();
            zoomCanvas = document.createElement('div');
            zoomCanvas.id = 'zoom-canvas';
            zoomCanvas.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;z-index:999;cursor:crosshair;';
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
        zRect.style.cssText = 'position:absolute;border:2px solid #007aff;background:rgba(0,122,255,0.08);pointer-events:none;z-index:1000;';
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
        const x1 = Math.min(zStart.x, ex), x2 = Math.max(zStart.x, ex);
        const y1 = Math.min(zStart.y, ey), y2 = Math.max(zStart.y, ey);
        zStart = null; zRect = null;

        if (zoomCanvas) { zoomCanvas.remove(); zoomCanvas = null; }
        zoomMode = false;
        zoomBtn.style.background = 'rgba(255,255,255,0.06)';
        zoomBtn.style.color = 'rgba(255,255,255,0.7)';
        zoomBtn.textContent = '🔍 放大';

        if (x2 - x1 < 0.03 || y2 - y1 < 0.03) return;

        const data = getChatData(currentActiveImage);
        const bbox = data.bbox;
        if (!bbox) { showToast('缺少坐标信息', 'error'); return; }

        const subMinLng = bbox.min_lng + (bbox.max_lng - bbox.min_lng) * x1;
        const subMaxLng = bbox.min_lng + (bbox.max_lng - bbox.min_lng) * x2;
        const subMinLat = bbox.min_lat + (bbox.max_lat - bbox.min_lat) * y1;
        const subMaxLat = bbox.min_lat + (bbox.max_lat - bbox.min_lat) * y2;

        showToast('正在获取放大影像...', 'info');
        try {
            const r2 = await fetch("http://127.0.0.1:8000/api/satellite/get-img/", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ min_lng: subMinLng, max_lng: subMaxLng, min_lat: subMinLat, max_lat: subMaxLat })
            });
            const d = await r2.json();
            if (d.code === 200) {
                const fileName = d.data.file_name;
                const imgUrl = `http://127.0.0.1:8000/api/satellite/show-img/?file=${fileName}`;
                const spatialCtx = d.data.gsd_m ? `范围: ${d.data.area_km2} km² | 分辨率: ${d.data.gsd_m} m/像素` : "";
                chatMemories[fileName] = { history: [], spatial: spatialCtx, bbox: { min_lng: subMinLng, max_lng: subMaxLng, min_lat: subMinLat, max_lat: subMaxLat } };
                modalImg.src = imgUrl;
                modalIdSpan.innerText = spatialCtx ? ` · ${spatialCtx}` : '';
                currentActiveImage = fileName;
                currentSpatialCtx = spatialCtx;
                renderChatHistory();
                showToast('放大影像已加载 ✓', 'success');
            }
        } catch(err) {}
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
        searchResults.innerHTML = '<div class="search-result-item" style="text-align:center;color:rgba(255,255,255,0.3);"><span class="spinner"></span> 搜索中...</div>';
        searchIdx = -1;
        searchTimeout = setTimeout(async () => {
            try {
                const r = await fetch(`http://127.0.0.1:8000/api/geo/search/?q=${encodeURIComponent(q)}`);
                const resp = await r.json();
                const data = resp.data || [];
                searchResults.innerHTML = '';
                if (data.length === 0) {
                    searchResults.innerHTML = '<div class="search-result-item" style="text-align:center;color:rgba(255,255,255,0.3);">无匹配结果</div>';
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
                searchResults.innerHTML = '<div class="search-result-item" style="text-align:center;color:rgba(255,255,255,0.3);">搜索服务不可用</div>';
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
            if (selectionRect) { map.removeLayer(selectionRect); selectionRect = null; }
            if (isSelecting) { isSelecting = false; map.dragging.enable(); }
            if (modal.style.display === 'flex') { modal.style.display = 'none'; }
            showToast('已取消', 'info');
        }
    });

    // ==========================================
    // 聊天历史
    // ==========================================
    const historyList = document.getElementById('history-list');
    const refreshHistoryBtn = document.getElementById('refresh-history-btn');

    async function loadHistories() {
        try {
            const r = await fetch('http://127.0.0.1:8000/api/ai/history/');
            const d = await r.json();
            if (d.code !== 200 || !d.data.length) {
                historyList.innerHTML = '<div style="font-size:11px;color:rgba(255,255,255,0.25);text-align:center;padding:10px 0;">暂无历史记录</div>';
                return;
            }
            historyList.innerHTML = '';
            d.data.forEach(h => {
                const item = document.createElement('div');
                item.style.cssText = 'display:flex; justify-content:space-between; align-items:center; padding:4px 0; font-size:11px; color:rgba(255,255,255,0.6); cursor:pointer;';
                const label = document.createElement('span');
                label.textContent = (h.spatial_context || h.image_file).substring(0, 30);
                label.title = '点击加载';
                label.addEventListener('click', async () => {
                    const rr = await fetch(`http://127.0.0.1:8000/api/ai/history/${h.id}/`);
                    const dd = await rr.json();
                    if (dd.code === 200) {
                        const fileName = dd.data.image_file;
                        const imgUrl = `http://127.0.0.1:8000/api/satellite/show-img/?file=${fileName}`;
                        currentActiveImage = fileName;
                        currentSpatialCtx = dd.data.spatial_context || '';
                        chatMemories[fileName] = { history: dd.data.messages || [], spatial: dd.data.spatial_context || '', bbox: dd.data.bbox };
                        modalImg.src = imgUrl;
                        modalIdSpan.innerText = currentSpatialCtx ? ' · ' + currentSpatialCtx : '';
                        renderChatHistory();
                        modal.style.display = 'flex';
                        if (dd.data.bbox) {
                            const b = dd.data.bbox;
                            map.fitBounds([[b.min_lat, b.min_lng], [b.max_lat, b.max_lng]]);
                        }
                        showToast('已加载历史对话', 'info');
                    }
                });
                const delBtn = document.createElement('button');
                delBtn.textContent = '✕';
                delBtn.style.cssText = 'background:none;border:none;color:rgba(255,255,255,0.2);cursor:pointer;font-size:10px;';
                delBtn.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    await fetch(`http://127.0.0.1:8000/api/ai/history/${h.id}/`, { method: 'DELETE' });
                    loadHistories();
                });
                item.appendChild(label);
                item.appendChild(delBtn);
                historyList.appendChild(item);
            });
        } catch (e) {}
    }
    refreshHistoryBtn.addEventListener('click', loadHistories);

    async function saveHistory(fileName) {
        const mem = chatMemories[fileName] || {};
        const bbox = mem.bbox ? mem.bbox : {};
        try {
            await fetch('http://127.0.0.1:8000/api/ai/history/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    image_file: fileName,
                    messages: mem.history || [],
                    spatial_context: mem.spatial || '',
                    bbox: bbox
                })
            });
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
            const r = await fetch('http://127.0.0.1:8000/api/report/generate/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    file_name: currentActiveImage,
                    title: 'SatelliteSense 遥感分析报告',
                    messages: mem.history || [],
                    spatial_context: mem.spatial || '',
                    bbox: mem.bbox || {}
                })
            });
            const d = await r.json();
            if (d.code === 200) {
                window.open('http://127.0.0.1:8000' + d.data.download_url);
                showToast('报告已生成，正在下载', 'success');
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
