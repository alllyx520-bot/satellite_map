import { lazy, Suspense, useEffect, useRef, useState, forwardRef, useImperativeHandle } from "react";
import {
  Map as MapIcon,
  Image,
  Columns2,
  Maximize2,
  Minimize2,
  Layers,
  Crosshair,
  MousePointer2,
  Scan,
  LoaderCircle,
  LocateFixed,
  SlidersHorizontal,
  ChevronsLeftRight,
  TriangleAlert,
  Plus,
  Minus,
  Maximize,
} from "lucide-react";
import type { Detail, Observation, Attachment } from "./types";
import { useWorkspace } from "./store";
import { client } from "./api";
import type { ViewState } from "./ImageCanvas";
import {hasGeographicView} from './geo';
const ImageCanvas = lazy(() => import("./ImageCanvas"));
export type BasemapHandle = { reset: () => void };
const Basemap = forwardRef<
  BasemapHandle,
  { detail: Detail; onChange: () => void; onError: (x: string) => void }
>(function Basemap({ detail, onChange, onError }, ref) {
  const host = useRef<HTMLDivElement>(null),
    mapRef = useRef<any>(null),
    drawRef = useRef<any>(null),
    tileSourceRef = useRef<any>(null),
    initialViewRef = useRef<{ center: number[]; zoom: number } | null>(null),
    [drawing, setDrawing] = useState(false),
    [place, setPlace] = useState(""),
    [searching, setSearching] = useState(false),
    [mapReady, setMapReady] = useState(0),
    [tilesLoading, setTilesLoading] = useState(true),
    [tilesFailed, setTilesFailed] = useState(false),
    [status, setStatus] = useState<{ zoom: number; res: number; lon: number; lat: number } | null>(null);
  const state = useWorkspace();
  const hoverAt = useRef(0);
  useImperativeHandle(ref, () => ({
    reset: () => {
      const map = mapRef.current,
        initial = initialViewRef.current;
      if (map && initial)
        map.getView().animate({
          center: initial.center,
          zoom: initial.zoom,
          duration: 500,
        });
    },
  }));
  useEffect(() => {
    let map: any;
    let disposed = false;
    (async () => {
      const [
        { default: Map },
        { default: View },
        { default: TileLayer },
        { default: XYZ },
        { default: VectorLayer },
        { default: VectorSource },
        { default: DragBox },
        { default: ScaleLine },
        { fromLonLat, toLonLat, transformExtent },
      ] = await Promise.all([
        import("ol/Map"),
        import("ol/View"),
        import("ol/layer/Tile"),
        import("ol/source/XYZ"),
        import("ol/layer/Vector"),
        import("ol/source/Vector"),
        import("ol/interaction/DragBox"),
        import("ol/control/ScaleLine"),
        import("ol/proj"),
      ]);
      if (disposed || !host.current) return;
      const vectors = new VectorSource();
      const evidenceVectors = new VectorSource();
      const tileSource = new XYZ({
        url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attributions: "© Esri · 影像拍摄日期未知",
        crossOrigin: "anonymous",
      });
      tileSourceRef.current = tileSource;
      let pending = 0;
      const bump = (delta: number) => {
        pending = Math.max(0, pending + delta);
        if (!disposed) setTilesLoading(pending > 0);
      };
      tileSource.on("tileloadstart", () => bump(1));
      tileSource.on("tileloadend", () => bump(-1));
      tileSource.on("tileloaderror", () => {
        bump(-1);
        if (!disposed) setTilesFailed(true);
      });
      const initialCenter = state.mapView?.center || fromLonLat([112.4, 30.5]);
      const initialZoom = state.mapView?.zoom ?? 5;
      initialViewRef.current = { center: initialCenter, zoom: initialZoom };
      map = new Map({
        target: host.current,
        layers: [
          new TileLayer({ source: tileSource }),
          new VectorLayer({ source: vectors }),
          new VectorLayer({source:evidenceVectors, style:{'stroke-color':'#d1b885','stroke-width':1.4,'fill-color':'rgba(209,184,133,0.06)','circle-radius':4.5,'circle-fill-color':'#df916e','circle-stroke-color':'rgba(255,255,255,0.2)','circle-stroke-width':1}}),
        ],
        view: new View({ center: initialCenter, zoom: initialZoom }),
      });
      map.addControl(new ScaleLine({ units: "metric" }));
      mapRef.current = map;
      map.set('evidenceSource',evidenceVectors);
      const [initLon, initLat] = toLonLat(initialCenter);
      setStatus({ zoom: initialZoom, res: map.getView().getResolution() || 0, lon: initLon, lat: initLat });
      map.on('pointermove', (event: any) => {
        if (event.dragging) return;
        const now = Date.now();
        if (now - hoverAt.current < 120) return;
        hoverAt.current = now;
        const [lon, lat] = toLonLat(event.coordinate);
        const view = map.getView();
        setStatus({ zoom: view.getZoom() ?? 0, res: view.getResolution() || 0, lon, lat });
      });
      setMapReady(value=>value+1);
      map.on('moveend',()=>{
        state.set({mapView:{center:map.getView().getCenter(),zoom:map.getView().getZoom()}});
        const view=map.getView();
        const [lon,lat]=toLonLat(view.getCenter());
        setStatus({zoom:view.getZoom()??0,res:view.getResolution()||0,lon,lat});
      });
      // DragBox gives map selection the same direct press-drag-release gesture
      // described by the UI. Draw's createBox() only emulates that gesture after
      // entering a drawing interaction and caused clicks to be misinterpreted.
      const draw = new DragBox();
      draw.setActive(false);
      map.addInteraction(draw);
      drawRef.current = draw;
      draw.on("boxend", async () => {
        draw.setActive(false);
        setDrawing(false);
        try {
          const bbox = transformExtent(
            draw.getGeometry().getExtent(),
            "EPSG:3857",
            "EPSG:4326",
          );
          const r = await client.region({
            name: "地图选区 " + (detail.attachments.length + 1),
            bbox,
            crs: "EPSG:4326",
            source: "esri",
            conversation_id: detail.conversation.id,
          });
          state.addDraft(r.attachment);
          state.set({
            selectedAttachment: r.attachment.id,
            canvasMode: "image",
          });
          onChange();
        } catch (error) {
          onError((error as Error).message);
        }
      });
      const resize = new ResizeObserver(() => map.updateSize());
      resize.observe(host.current);
      map.set("resizeObserver", resize);
    })();
    return () => {
      disposed = true;
      map?.get("resizeObserver")?.disconnect();
      map?.setTarget(undefined);
      map?.dispose();
    };
  }, [detail.conversation.id]);
  useEffect(()=>{
    const map=mapRef.current;
    if(!map||!state.mapTarget)return;
    import('ol/proj').then(({transformExtent})=>{
      if(mapRef.current===map)map.getView().fit(transformExtent(state.mapTarget!,'EPSG:4326','EPSG:3857'),{padding:[80,70,80,70],duration:500,maxZoom:17});
    });
  },[state.mapTarget,mapReady]);
  useEffect(()=>{
    const map=mapRef.current;
    if(!map)return;
    import('ol/format/GeoJSON').then(({default:GeoJSON})=>{
      if(mapRef.current!==map)return;
      const source=map.get('evidenceSource');source.clear();
      const features:any[]=[];
      for(const evidence of detail.evidence||[]){
        const b=evidence.bbox;
        if(b&&[b.min_lng,b.min_lat,b.max_lng,b.max_lat].every(Number.isFinite))features.push({type:'Feature',properties:{evidence_id:evidence.id},geometry:{type:'Polygon',coordinates:[[[b.min_lng,b.min_lat],[b.max_lng,b.min_lat],[b.max_lng,b.max_lat],[b.min_lng,b.max_lat],[b.min_lng,b.min_lat]]]}});
        for(const fire of evidence.value?.top_frp_fires||[])features.push({type:'Feature',properties:{evidence_id:evidence.id},geometry:{type:'Point',coordinates:[fire.longitude,fire.latitude]}});
      }
      source.addFeatures(new GeoJSON().readFeatures({type:'FeatureCollection',features},{dataProjection:'EPSG:4326',featureProjection:'EPSG:3857'}));
    });
  },[detail.evidence,mapReady]);
  async function search() {
    if (!place.trim()) return;
    setSearching(true);
    try {
      const direct = place
        .trim()
        .match(/^\s*([-+]?\d+(?:\.\d+)?)\s*[,，\s]\s*([-+]?\d+(?:\.\d+)?)\s*$/);
      let lng: number, lat: number;
      if (direct) {
        lng = Number(direct[1]);
        lat = Number(direct[2]);
      } else {
        const data = await client.places(place.trim());
        const item =
          data.items?.[0] ||
          data.results?.[0] ||
          data.data?.[0] ||
          data.data ||
          data;
        lng = Number(
          item?.lng ??
            item?.lon ??
            item?.longitude ??
            item?.center?.[0] ??
            item?.coordinates?.[0],
        );
        lat = Number(
          item?.lat ??
            item?.latitude ??
            item?.center?.[1] ??
            item?.coordinates?.[1],
        );
      }
      if (
        !Number.isFinite(lng!) ||
        !Number.isFinite(lat!) ||
        Math.abs(lng!) > 180 ||
        Math.abs(lat!) > 90
      )
        throw new Error("请输入“经度, 纬度”，或输入可定位的地点名称");
      const [{ fromLonLat }] = await Promise.all([import("ol/proj")]);
      mapRef.current
        ?.getView()
        .animate({ center: fromLonLat([lng!, lat!]), zoom: 12, duration: 500 });
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setSearching(false);
    }
  }
  const hasEvidenceExtent = (detail.evidence || []).some((e) => {
    const b = e.bbox;
    return b && [b.min_lng, b.min_lat, b.max_lng, b.max_lat].every(Number.isFinite);
  });
  const hasFires = (detail.evidence || []).some(
    (e) => (e.value?.top_frp_fires || []).length > 0,
  );
  return (
    <>
      <div
        className={"basemap" + (drawing ? " drawing" : "")}
        ref={host}
        aria-label="卫星影像地图"
      />
      {tilesLoading && (
        <div className="canvas-loading">
          <LoaderCircle className="spin" size={20} />
          <span>正在加载影像…</span>
        </div>
      )}
      {tilesFailed && (
        <button
          className="canvas-error center"
          role="alert"
          onClick={() => {
            setTilesFailed(false);
            setTilesLoading(true);
            tileSourceRef.current?.refresh();
          }}
        >
          影像底图加载失败，点击重试
        </button>
      )}
      {(hasEvidenceExtent || hasFires) && (
        <div className="map-legend">
          {hasEvidenceExtent && (
            <span>
              <i className="legend-box" />
              证据范围
            </span>
          )}
          {hasFires && (
            <span>
              <i className="legend-fire" />
              火点
            </span>
          )}
        </div>
      )}
      {status && (
        <div className="canvas-status">
          Z {status.zoom.toFixed(0)} · {status.res.toFixed(1)} m/px ·{" "}
          {status.lon.toFixed(4)},{status.lat.toFixed(4)}
        </div>
      )}
      <div className="map-search">
        <LocateFixed size={15} />
        <input
          aria-label="搜索地点或经纬度"
          placeholder="地点，或经度, 纬度"
          value={place}
          onChange={(e) => setPlace(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) search();
          }}
        />
        <button onClick={search} aria-label="定位地点">
          {searching ? (
            <LoaderCircle className="spin" size={14} />
          ) : (
            <span>定位</span>
          )}
        </button>
      </div>
      <div className="map-tools">
        <button
          className={!drawing ? "selected" : ""}
          aria-label="平移地图"
          onClick={() => {
            setDrawing(false);
            drawRef.current?.setActive(false);
          }}
        >
          <MousePointer2 size={17} />
          <span>平移</span>
        </button>
        <button
          className={drawing ? "selected" : ""}
          aria-label="框选区域"
          onClick={() => {
            setDrawing(!drawing);
            drawRef.current?.setActive(!drawing);
          }}
        >
          <Scan size={18} />
          <span>框选区域</span>
        </button>
      </div>
      <div className={"canvas-tip" + (drawing ? " is-drawing" : "")}>
        {drawing
          ? "在地图点击并拖动框选，松开后创建区域影像"
          : "先探索地图，再框选你想了解的区域"}
      </div>
    </>
  );
});
export function Canvas({
  detail,
  onChange,
}: {
  detail: Detail;
  onChange: () => void;
}) {
  const s = useWorkspace(),
    [full, setFull] = useState(false),
    [error, setError] = useState(""),
    [hover, setHover] = useState<{ x: number; y: number } | null>(null);
  const basemapRef = useRef<BasemapHandle>(null),
    paneMaps = useRef<(any | null)[]>([null, null]),
    panesRef = useRef<HTMLDivElement>(null);
  const a =
      detail.attachments.find((asset) => asset.id === s.selectedAttachment) ||
      detail.attachments[0],
    b =
      detail.attachments.find(
        (asset) => asset.id === s.secondAttachment && asset.id !== a?.id,
      ) || detail.attachments.find((asset) => asset.id !== a?.id),
    o = detail.observations.find((o) => o.id === s.selectedObservation);
  const last = useRef("");
  useEffect(() => {
    const latest = detail.observations.at(-1);
    if (latest && latest.id !== last.current) {
      last.current = latest.id;
      if (s.follow)
        s.set({
          selectedObservation: latest.id,
          selectedAttachment: latest.attachment_id,
          canvasMode: "image",
        });
    }
  }, [detail.observations.length, s.follow]);
  function observation(o: Observation) {
    s.set({
      selectedObservation: o.id,
      selectedAttachment: o.attachment_id,
      references: [...s.references.filter((id) => id !== o.id), o.id],
    });
    onChange();
  }
  const image = (asset: Attachment | undefined, paneIndex = 0) =>
    asset?.status === "ready" && asset.tile_url ? (
      <ImageCanvas
        attachment={asset}
        observations={detail.observations.filter(
          (o) => o.attachment_id === asset.id,
        )}
        selected={s.selectedObservation}
        onSelect={(id) =>
          s.set({ selectedObservation: id, selectedAttachment: asset.id })
        }
        onObservation={observation}
        onManualPan={() => s.set({ follow: false })}
        onError={setError}
        onViewChange={(canvasView) => s.set({ canvasView })}
        linkedView={s.canvasView}
        onHover={setHover}
        onMapReady={(map) => {
          paneMaps.current[paneIndex] = map;
        }}
      />
    ) : (
      <div className="canvas-empty">
        <Image size={26} />
        <strong>{asset?.name || "尚未添加影像"}</strong>
        <p>{asset?.error || "添加影像后，可逐级放大到原始像素。"}</p>
        {asset && asset.status !== "failed" && (
          <span className="canvas-progress">
            <span className="indeterminate" aria-hidden="true" />
            正在准备瓦片
          </span>
        )}
      </div>
    );
  function zoomPanes(direction: 1 | -1) {
    for (const map of paneMaps.current) {
      if (!map) continue;
      const view = map.getView();
      view.animate({ zoom: (view.getZoom() ?? 1) + direction, duration: 150 });
    }
  }
  function dividerDrag(event: React.PointerEvent) {
    const panes = panesRef.current;
    if (!panes) return;
    event.preventDefault();
    const move = (ev: PointerEvent) => {
      const rect = panes.getBoundingClientRect();
      const pct = ((ev.clientX - rect.left) / rect.width) * 100;
      s.set({ compareSplit: Math.round(Math.min(96, Math.max(4, pct)) * 10) / 10 });
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }
  return (
    <section
      className={"canvas " + (full ? "canvas-full" : "")}
      aria-label="影像工作区"
    >
      <header className="canvasbar">
        <div className="canvas-tabs">
          <button
            className={s.canvasMode === "map" ? "selected" : ""}
            onClick={() => s.set({ canvasMode: "map" })}
          >
            <MapIcon size={15} />
            地图
          </button>
          <button
            className={s.canvasMode === "image" ? "selected" : ""}
            onClick={() => s.set({ canvasMode: "image" })}
          >
            <Image size={15} />
            影像
          </button>
          <button
            disabled={detail.attachments.length < 2}
            className={s.canvasMode === "compare" ? "selected" : ""}
            onClick={() => s.set({ canvasMode: "compare" })}
          >
            <Columns2 size={15} />
            对比
          </button>
        </div>
        <div className="canvas-actions">
          {s.canvasMode === "map" && (
            <button
              title="复位视野"
              aria-label="复位视野"
              onClick={() => basemapRef.current?.reset()}
            >
              <Maximize size={15} />
            </button>
          )}
          <button
            title="自动定位 Agent 新观察的位置；手动移动会暂停跟随"
            className={s.follow ? "selected" : ""}
            onClick={() => s.set({ follow: !s.follow })}
          >
            <Crosshair size={15} />
            <span>跟随</span>
          </button>
          <button
            aria-label="证据与图层"
            onClick={() => s.set({ drawer: !s.drawer })}
          >
            <Layers size={17} />
          </button>
          <button
            aria-label={full ? "退出全屏" : "全屏画布"}
            onClick={() => setFull(!full)}
          >
            {full ? <Minimize2 size={17} /> : <Maximize2 size={17} />}
          </button>
        </div>
      </header>
      <div className="canvas-body">
        {s.canvasMode === "map" ? (
          <Basemap
            ref={basemapRef}
            detail={detail}
            onChange={onChange}
            onError={setError}
          />
        ) : (
          <>
            <div className="asset-selector">
              <select
                aria-label="当前影像"
                value={a?.id || ""}
                onChange={(e) =>
                  s.set({
                    selectedAttachment: e.target.value,
                    selectedObservation: undefined,
                  })
                }
              >
                {detail.attachments.map((a) => (
                  <option value={a.id} key={a.id}>
                    {a.name}
                  </option>
                ))}
              </select>
              {s.canvasMode === "compare" && (
                <>
                  <select
                    aria-label="对比影像"
                    value={b?.id || ""}
                    onChange={(e) =>
                      s.set({ secondAttachment: e.target.value })
                    }
                  >
                    {detail.attachments
                      .filter((x) => x.id !== a?.id)
                      .map((a) => (
                        <option value={a.id} key={a.id}>
                          {a.name}
                        </option>
                      ))}
                  </select>
                  <button
                    title="并排对比"
                    aria-label="并排对比"
                    className={!s.comparisonSlider ? "selected" : ""}
                    onClick={() => s.set({ comparisonSlider: false })}
                  >
                    <Columns2 size={14} />
                  </button>
                  <button
                    title="滑杆对比"
                    aria-label="滑杆对比"
                    className={s.comparisonSlider ? "selected" : ""}
                    onClick={() => s.set({ comparisonSlider: true })}
                  >
                    <SlidersHorizontal size={14} />
                  </button>
                </>
              )}
            </div>
            <Suspense
              fallback={<div className="canvas-empty">正在打开影像查看器…</div>}
            >
              <div
                ref={panesRef}
                className={
                  "image-panes " +
                  (s.canvasMode === "compare" ? "compare " : "") +
                  (s.comparisonSlider && s.canvasMode === "compare"
                    ? "slider"
                    : "")
                }
              >
                <div className="image-pane">
                  {s.canvasMode === "compare" && !s.comparisonSlider && (
                    <span className="pane-tag">A</span>
                  )}
                  {image(a, 0)}
                </div>
                {s.canvasMode === "compare" && (
                  <div
                    className="image-pane second"
                    style={
                      s.comparisonSlider
                        ? { clipPath: `inset(0 0 0 ${s.compareSplit}%)` }
                        : undefined
                    }
                  >
                    {!s.comparisonSlider && <span className="pane-tag">B</span>}
                    {image(b, 1)}
                  </div>
                )}
                {s.canvasMode === "compare" && s.comparisonSlider && (
                  <div
                    className="compare-divider"
                    style={{ left: s.compareSplit + "%" }}
                    onPointerDown={dividerDrag}
                    role="slider"
                    aria-label="拖动调整对比分割线"
                    aria-valuenow={Math.round(s.compareSplit)}
                    aria-valuemin={0}
                    aria-valuemax={100}
                  >
                    <span className="compare-handle">
                      <ChevronsLeftRight size={15} />
                    </span>
                  </div>
                )}
              </div>
            </Suspense>
            {s.canvasMode === "compare" && s.comparisonSlider && (
              <div className="compare-zoom">
                <button aria-label="同时放大两幅影像" onClick={() => zoomPanes(1)}>
                  <Plus size={15} />
                </button>
                <button aria-label="同时缩小两幅影像" onClick={() => zoomPanes(-1)}>
                  <Minus size={15} />
                </button>
              </div>
            )}
            {s.canvasMode === "compare" &&
              !(hasGeographicView(a) && hasGeographicView(b)) && (
                <div className="compare-warning" role="note">
                  <TriangleAlert size={12} />
                  两图未配准，位置同步仅供目视参考
                </div>
              )}
            {hover && (
              <div className="canvas-status">
                {hover.x}, {hover.y} px
              </div>
            )}
            <div className="canvas-tip">
              滚轮缩放 · 拖动平移 · Shift + 拖动框选局部
            </div>
          </>
        )}
        {o && s.canvasMode !== "map" && (
          <div className="observation-panel">
            <small>位置与证据</small>
            <strong>{o.label}</strong>
            <p className="obs-meta">
              {o.summary || `原图窗口 ${o.window.join(", ")}`}
            </p>
            <p className="obs-asset">
              {detail.attachments.find((x) => x.id === o.attachment_id)?.name ||
                "未命名影像"}
            </p>
            <button
              className="obs-ref"
              onClick={() =>
                s.set({
                  references: [...new Set([...s.references, o.id])],
                  mobile: "chat",
                })
              }
            >
              引用这个位置继续问 ↗
            </button>
          </div>
        )}
        {error && (
          <div className="canvas-error" role="alert">
            {error}
            <button onClick={() => setError("")} aria-label="关闭提示">
              ×
            </button>
          </div>
        )}
      </div>
    </section>
  );
}
