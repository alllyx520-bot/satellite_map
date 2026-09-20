import proj4 from 'proj4';
import type { Attachment } from './types';

export type GeoView = { center: number[]; right: number[] };

function projection(a: Attachment): string | undefined {
  if (a.coordinate_space !== 'geographic' || a.transform?.length !== 6) return;
  if (a.metadata.crs_wkt) return a.metadata.crs_wkt;
  const utm = a.crs?.match(/^EPSG:(326|327)(\d{2})$/);
  if (utm) return `+proj=utm +zone=${Number(utm[2])} ${utm[1] === '327' ? '+south' : ''} +datum=WGS84 +units=m +no_defs`;
  if (a.crs && proj4.defs(a.crs)) return a.crs;
}

export function hasGeographicView(a?: Attachment): boolean {
  return !!a && !!projection(a);
}

function toWorld(a: Attachment, x: number, y: number): number[] {
  const [p,q,r,s,t,u] = a.transform!;
  return [p*x+q*y+r, s*x+t*y+u];
}

function toPixel(a: Attachment, [x,y]: number[]): number[] {
  const [p,q,r,s,t,u] = a.transform!;
  const determinant = p*t-q*s;
  if (!determinant) throw new Error('不可逆的地理变换');
  return [(t*(x-r)-q*(y-u))/determinant, (-s*(x-r)+p*(y-u))/determinant];
}

export function pixelCoordinate(coordinate: number[]): { x: number; y: number } {
  return { x: Math.round(coordinate[0]), y: Math.round(-coordinate[1]) };
}

export function geographicView(a: Attachment, center: number[], resolution: number, rotation: number): GeoView | undefined {
  const crs = projection(a);
  if (!crs) return;
  try {
    const point = (x: number, y: number) => proj4(crs, 'EPSG:4326', toWorld(a,x,y));
    const result = {center: point(center[0], -center[1]), right: point(center[0]+resolution*Math.cos(rotation), -center[1]-resolution*Math.sin(rotation))};
    return [...result.center,...result.right].every(Number.isFinite) ? result : undefined;
  } catch { return; }
}

export function projectView(a: Attachment, geo?: GeoView) {
  const crs = projection(a);
  if (!crs || !geo) return;
  try {
    const center = toPixel(a, proj4('EPSG:4326', crs, geo.center));
    const right = toPixel(a, proj4('EPSG:4326', crs, geo.right));
    const dx = right[0]-center[0], dy = right[1]-center[1];
    return {center: [center[0], -center[1]], resolution: Math.hypot(dx,dy), rotation: Math.atan2(-dy,dx)};
  } catch { return; }
}
