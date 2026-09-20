import { ArrowUpRight, Database, MapPin } from 'lucide-react';
import type { DataEvidence as Evidence } from './types';
import { useWorkspace } from './store';

export const productNames: Record<string,string> = {python_analysis:'Python 计算', ndvi:'植被指数', ndwi:'水体指数', mndwi:'改进水体指数',
  ndbi:'建成区指数', bsi:'裸土指数', ndsi:'积雪指数', nbr:'燃烧指数', water_baseline:'历史水体背景',
  landcover:'土地覆盖', osm:'道路与建筑背景', weather:'同期气象', fire_detections:'卫星火点',
  dem_terrain:'地形', landsat_surface_temperature:'地表温度', sar_backscatter:'雷达散射'};

type FieldDef = {label:string; unit?:string};
const fieldDefs: Record<string,FieldDef> = {mean:{label:'均值'}, min:{label:'最小值'}, max:{label:'最大值'},
  valid_pixel_count:{label:'有效像元', unit:'个'}, valid_pixel_ratio:{label:'有效像元比例'},
  above_threshold_ratio:{label:'超过阈值的比例'}, mean_elevation_m:{label:'平均高程', unit:'m'},
  mean_slope_deg:{label:'平均坡度', unit:'°'}, max_slope_deg:{label:'最大坡度', unit:'°'},
  mean_celsius:{label:'平均温度', unit:'°C'}, min_celsius:{label:'最低温度', unit:'°C'},
  max_celsius:{label:'最高温度', unit:'°C'}, building_count:{label:'建筑要素数量', unit:'个'},
  road_count:{label:'道路要素数量', unit:'个'}, fire_count:{label:'火点数', unit:'个'},
  water_feature_count:{label:'水体要素数量', unit:'个'}, requested_area_sq_km:{label:'请求面积', unit:'km²'},
  valid_area_sq_km:{label:'有效面积', unit:'km²'}, common_valid_fraction:{label:'共同有效覆盖'},
  changed_fraction:{label:'超过变化阈值的比例'}};

const indexMetrics = new Set(['ndvi','ndwi','mndwi','ndbi','bsi','ndsi','nbr']);

export type StatField = {key:string; label:string; unit?:string; value:number; highlight:boolean};

export function statFields(metric:string, stats:Record<string,unknown>): StatField[] {
  return Object.entries(stats)
    .filter(([key,value])=>fieldDefs[key] && typeof value==='number')
    .map(([key,value])=>{
      const def = fieldDefs[key];
      const num = value as number;
      const highlight = (key==='fire_count' && num>0) || (key==='mean' && indexMetrics.has(metric) && num>0.6);
      return {key, label:def.label, unit:def.unit, value:num, highlight};
    });
}

export function DataEvidenceLink({evidence:e}:{evidence:Evidence}) {
  const s=useWorkspace();
  return <button className="evidence-link" onClick={()=>s.set({selectedEvidence:e.id,drawer:true})}><Database size={13}/>{productNames[e.metric]||e.metric}<span><ArrowUpRight size={11}/></span></button>;
}

export function DataEvidenceCard({evidence:e}:{evidence:Evidence}) {
  const s=useWorkspace();
  const source=e.data_contract?.metadata?.collection||e.data_contract?.source||e.value?.source;
  const date=e.data_contract?.metadata?.acquired_at||e.data_contract?.date;
  const stats=e.value?.summary||e.value||{};
  const fields=statFields(e.metric, stats);
  return <section className={'data-evidence '+(s.selectedEvidence===e.id?'selected':'')}>
    <strong>{productNames[e.metric]||e.metric}</strong>
    <p className="evidence-origin">{source||e.method}{date?' · '+String(date).slice(0,10):''}</p>
    {e.value?.available===false&&<p role="status">{e.value.reason||'数据未满足分析条件'}</p>}
    {fields.length>0&&<dl>{fields.map((f)=><div key={f.key}><dt>{f.label}</dt><dd className={f.highlight?'is-highlight':''}>{f.value.toLocaleString('zh-CN',{maximumFractionDigits:4})}{f.unit&&<small> {f.unit}</small>}</dd></div>)}</dl>}
    {(e.limitations||[]).map((text,i)=><p className="evidence-limitation" key={i}>{text}</p>)}
    {e.bbox&&<button className="outline-button" onClick={()=>s.set({canvasMode:'map',drawer:false,mobile:'canvas',mapTarget:[e.bbox!.min_lng,e.bbox!.min_lat,e.bbox!.max_lng,e.bbox!.max_lat]})}><MapPin size={14}/>在地图查看覆盖范围</button>}
    <details><summary>查看完整数据依据</summary><pre>{JSON.stringify(e.value,null,2)}</pre></details>
  </section>;
}
