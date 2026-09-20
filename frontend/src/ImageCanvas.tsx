import { useEffect, useRef, useState } from "react";
import Map from "ol/Map";
import View from "ol/View";
import TileLayer from "ol/layer/Tile";
import TileImage from "ol/source/TileImage";
import TileGrid from "ol/tilegrid/TileGrid";
import Projection from "ol/proj/Projection";
import VectorLayer from "ol/layer/Vector";
import VectorSource from "ol/source/Vector";
import Feature from "ol/Feature";
import Polygon from "ol/geom/Polygon";
import Style from "ol/style/Style";
import Stroke from "ol/style/Stroke";
import Fill from "ol/style/Fill";
import DragBox from "ol/interaction/DragBox";
import { shiftKeyOnly } from "ol/events/condition";
import type { Attachment, Observation } from "./types";
import { client } from "./api";
import { geographicView, projectView, pixelCoordinate } from './geo';
import type { GeoView } from './geo';
export type ViewState = {
  x: number;
  y: number;
  scale: number;
  rotation: number;
  source: string;
  geo?: GeoView;
};
type Props = {
  attachment: Attachment;
  observations: Observation[];
  selected?: string;
  onSelect: (id: string) => void;
  onObservation: (o: Observation) => void;
  onViewChange?: (v: ViewState) => void;
  linkedView?: ViewState;
  onManualPan: () => void;
  onError: (message: string) => void;
  onHover?: (p: { x: number; y: number } | null) => void;
  onMapReady?: (map: Map | null) => void;
};
const extent = (w: number[]) => [w[0], -(w[1] + w[3]), w[0] + w[2], -w[1]];
export function ImageCanvas(props: Props) {
  const { attachment: a, observations, selected, linkedView } = props;
  const host = useRef<HTMLDivElement>(null),
    mapRef = useRef<Map | null>(null),
    vectorRef = useRef<VectorSource | null>(null);
  const live = useRef(props);
  live.current = props;
  const syncing = useRef(false);
  const publishView = useRef<() => void>(() => {});
  const lastFitted = useRef('');
  const [ready, setReady] = useState(0);
  const [dragInfo, setDragInfo] = useState<{
    left: number;
    top: number;
    width: number;
    height: number;
  } | null>(null);
  useEffect(() => {
    if (!host.current || !a.tile_url) return;
    const bounds = [0, -a.height, a.width, 0],
      resolutions = a.metadata.resolutions || [1];
    const projection = new Projection({
      code: "PIXELS:" + a.id,
      units: "pixels",
      extent: bounds,
    });
    const grid = new TileGrid({
      extent: bounds,
      origin: [0, 0],
      resolutions,
      tileSize: 256,
    });
    const source = new TileImage({
      projection,
      tileGrid: grid,
      wrapX: false,
      tileUrlFunction: (c) =>
        a
          .tile_url!.replace("{z}", String(c[0]))
          .replace("{x}", String(c[1]))
          .replace("{y}", String(c[2])),
    });
    source.on("tileloaderror", () =>
      live.current.onError("部分影像瓦片加载失败，可重新打开影像重试。"),
    );
    const vector = new VectorSource();
    vectorRef.current = vector;
    const layers = [
      new TileLayer({ source }),
      new VectorLayer({
        source: vector,
        style: (f) =>
          new Style({
            stroke: new Stroke({
              color:
                f.getId() === live.current.selected ? "#f0d296" : "#bfa46c",
              width: f.getId() === live.current.selected ? 2.5 : 1.2,
            }),
            fill: new Fill({ color: "rgba(218,181,114,.045)" }),
          }),
      }),
    ];
    const view = new View({
      projection,
      extent: bounds,
      center: [a.width / 2, -a.height / 2],
      resolution: resolutions[0],
      maxResolution: resolutions[0] * 2,
      minResolution: 0.25,
      constrainOnlyCenter: true,
    });
    const map = new Map({ target: host.current, layers, view });
    mapRef.current = map;
    live.current.onMapReady?.(map);
    // On a narrow workspace the canvas can become visible only after React has
    // mounted the mobile pane. OpenLayers otherwise retains its initial 0px
    // target size and leaves a black surface until the user resizes the window.
    const refreshSize = () => map.updateSize();
    const frame = requestAnimationFrame(refreshSize);
    const settleTimer = window.setTimeout(refreshSize, 80);
    view.fit(bounds, { padding: [35, 35, 35, 35] });
    const saved = live.current.linkedView;
    if (saved?.source === a.id) {
      view.setCenter([saved.x*a.width, -saved.y*a.height]);
      view.setResolution(saved.scale*a.width);
      view.setRotation(saved.rotation);
      lastFitted.current = a.id + ':' + selected;
    }
    setReady((x) => x + 1);
    const sizeObserver = new ResizeObserver(() => map.updateSize());
    sizeObserver.observe(host.current);
    map.on("pointerdrag", () => live.current.onManualPan());
    const wheel = () => live.current.onManualPan();
    host.current.addEventListener("wheel", wheel, { passive: true });
    publishView.current = () => {
      const center = view.getCenter()!;
      live.current.onViewChange?.({
        x: center[0] / a.width,
        y: -center[1] / a.height,
        scale: (view.getResolution() || 1) / a.width,
        rotation: view.getRotation(),
        source: a.id,
        geo: geographicView(a, center, view.getResolution() || 1, view.getRotation()),
      });
    };
    map.on("moveend", () => { if (!syncing.current) publishView.current(); });
    map.on("pointermove", (event) => {
      if (event.dragging || !live.current.onHover) return;
      const p = pixelCoordinate(event.coordinate);
      live.current.onHover({
        x: Math.min(a.width, Math.max(0, p.x)),
        y: Math.min(a.height, Math.max(0, p.y)),
      });
    });
    map.getViewport().addEventListener("pointerleave", () =>
      live.current.onHover?.(null),
    );
    map.on("singleclick", (event) => {
      const feature = map.forEachFeatureAtPixel(event.pixel, (f) => f);
      if (feature?.getId()) live.current.onSelect(String(feature.getId()));
    });
    const box = new DragBox({ condition: shiftKeyOnly });
    map.addInteraction(box);
    box.on("boxdrag", () => {
      const geometry = box.getGeometry();
      if (!geometry) return;
      const e = geometry.getExtent();
      const pixel = map.getPixelFromCoordinate([(e[0] + e[2]) / 2, e[1]]);
      setDragInfo({
        left: pixel[0],
        top: pixel[1] + 8,
        width: Math.abs(Math.round(e[2] - e[0])),
        height: Math.abs(Math.round(e[3] - e[1])),
      });
    });
    box.on("boxcancel", () => setDragInfo(null));
    box.on("boxend", async () => {
      setDragInfo(null);
      const e = box.getGeometry().getExtent();
      const x = Math.max(0, Math.floor(e[0])),
        y = Math.max(0, Math.floor(-e[3]));
      const width = Math.min(a.width - x, Math.ceil(e[2]) - x),
        height = Math.min(a.height - y, Math.ceil(-e[1]) - y);
      if (width < 1 || height < 1) return;
      try {
        const r = await client.window(a.id, {
          x,
          y,
          width,
          height,
          max_size: 1024,
        });
        live.current.onObservation(r.observation);
      } catch (error) {
        live.current.onError((error as Error).message);
      }
    });
    return () => {
      sizeObserver.disconnect();
      cancelAnimationFrame(frame);
      window.clearTimeout(settleTimer);
      host.current?.removeEventListener("wheel", wheel);
      live.current.onMapReady?.(null);
      live.current.onHover?.(null);
      map.setTarget(undefined);
      map.dispose();
      mapRef.current = null;
    };
  }, [a.id, a.tile_url]);
  useEffect(() => {
    const vector = vectorRef.current;
    if (!vector) return;
    vector.clear();
    for (const o of observations) {
      if (o.window.length !== 4 || (o.kind === 'window' && o.metadata?.call_key && o.id !== selected)) continue;
      const [x1, y1, x2, y2] = extent(o.window);
      const feature = new Feature(
        new Polygon([
          [
            [x1, y1],
            [x2, y1],
            [x2, y2],
            [x1, y2],
            [x1, y1],
          ],
        ]),
      );
      feature.setId(o.id);
      vector.addFeature(feature);
    }
  }, [observations, selected, ready]);
  useEffect(() => {
    const map = mapRef.current,
      o = observations.find((o) => o.id === selected);
    if (!map || !o) return;
    const focusKey = a.id + ':' + selected;
    if (lastFitted.current === focusKey) return;
    lastFitted.current = focusKey;
    syncing.current = true;
    map.getView().fit(extent(o.window), {
      padding: [60, 60, 60, 60],
      duration: 160,
      callback: () => {
        syncing.current = false;
        publishView.current();
      },
    });
    vectorRef.current?.changed();
  }, [selected, ready]);
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !linkedView || linkedView.source === a.id) return;
    syncing.current = true;
    const view = map.getView();
    const projected = projectView(a, linkedView.geo);
    const release = () => {
      syncing.current = false;
    };
    map.once("moveend", release);
    view.setCenter(projected?.center || [linkedView.x * a.width, -linkedView.y * a.height]);
    view.setResolution(projected?.resolution || linkedView.scale * a.width);
    view.setRotation(projected?.rotation ?? linkedView.rotation);
    window.setTimeout(release, 250);
  }, [linkedView, ready]);
  return (
    <>
      <div
        className="pixelCanvas"
        ref={host}
        tabIndex={0}
        aria-label={a.name + " 可缩放影像，Shift 拖动框选局部"}
      />
      {dragInfo && (
        <div
          className="dragbox-size"
          style={{ left: dragInfo.left, top: dragInfo.top }}
        >
          {dragInfo.width} × {dragInfo.height} px
        </div>
      )}
    </>
  );
}
export default ImageCanvas;
