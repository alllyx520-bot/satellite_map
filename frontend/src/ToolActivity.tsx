import {useQuery} from '@tanstack/react-query';
import {client} from './api';

const toolNames: Record<string,string> = {
  load_capabilities:'加载能力', find_place:'定位地点', search_scenes:'检索影像场景',
  retrieve_imagery:'获取影像', compute_product:'计算数据产品', compare_two_date_change:'两期变化分析',
  consult_method:'查询方法', annotate_observation:'标注观察', read_image_window:'查看原图窗口',
  view_overview:'查看概览', spatial_relation:'空间关系分析', review_visual:'视觉复核',
  read_history:'回顾上下文', update_plan:'更新计划', finish_answer:'生成回答', python_analysis:'Python 分析',
  external_evidence:'获取外部证据',
};

export function toolName(name: string) {
  return toolNames[name] ?? name.replace(/_/g,' ');
}

export function toolRuntime(tool: {name: string; result?: {runtime?: unknown}}) {
  const runtime = tool.result?.runtime;
  return tool.name === 'python_analysis' && typeof runtime === 'string' ? runtime : undefined;
}

export function runtimeLabel(runtime?: string) {
  return runtime === 'local' ? '本地执行' : runtime;
}

type Tool = {id:number; name:string; status:string; arguments?:unknown; result?:{runtime?:unknown; error?:unknown}};

export type ToolStatus = {key:string; label:string; cls:string; dot:string};

export function toolStatus(tool: {status:string; result?:{error?:unknown}}): ToolStatus {
  if(tool.result?.error) return {key:'adjust', label:'需要调整', cls:'st-adjust', dot:'adjust'};
  if(tool.status==='completed') return {key:'done', label:'完成', cls:'st-done', dot:'completed'};
  if(tool.status==='uncertain') return {key:'unknown', label:'结果未知', cls:'st-unknown', dot:'adjust'};
  if(tool.status==='running') return {key:'running', label:'执行中', cls:'st-running', dot:'running'};
  if(tool.status==='error') return {key:'error', label:'错误', cls:'st-error', dot:'error'};
  return {key:tool.status, label:tool.status, cls:'', dot:''};
}

export type ToolGroup = {name:string; status:string; items:Tool[]};

export function groupTools(tools: Tool[]): ToolGroup[] {
  const groups: ToolGroup[] = [];
  for(const tool of tools){
    const status = toolStatus(tool).key;
    const last = groups[groups.length-1];
    if(last && last.name===tool.name && last.status===status) last.items.push(tool);
    else groups.push({name:tool.name, status, items:[tool]});
  }
  return groups;
}

function renderJson(value: unknown) {
  return JSON.stringify(value,null,2).replace(/\\r\\n/g,'\n').replace(/\\n/g,'\n');
}

function ToolSummary({tool, count}:{tool:Tool; count?:number}) {
  const status = toolStatus(tool);
  const runtime = toolRuntime(tool);
  return <summary>
    <span className={'tool-dot '+status.dot}></span>
    <span>{toolName(tool.name)}{count && count>1 ? ` ×${count}` : ''}</span>
    <small className={status.cls} title={runtime}>{runtime ? `${runtimeLabel(runtime)} · ` : ''}{status.label}</small>
  </summary>;
}

function ToolBody({tool}:{tool:Tool}) {
  return <>
    <p className="section-label">输入</p><pre>{renderJson(tool.arguments)}</pre>
    <p className="section-label">执行结果</p><pre>{renderJson(tool.result).slice(0,12000)}</pre>
  </>;
}

export function ToolActivity({runId}:{runId:number}) {
  const {data,error}=useQuery({queryKey:['run',runId],queryFn:()=>client.run(runId)});
  if(error)return <p className="error">暂时无法加载执行记录。</p>;
  const groups = groupTools(data?.tools ?? []);
  return <div className="tool-activity">{groups.map((group)=> group.items.length===1
    ? <details key={group.items[0].id}>
        <ToolSummary tool={group.items[0]}/>
        <ToolBody tool={group.items[0]}/>
      </details>
    : <details key={group.items[0].id} className="tool-group">
        <ToolSummary tool={group.items[0]} count={group.items.length}/>
        {group.items.map(tool=><details key={tool.id}>
          <ToolSummary tool={tool}/>
          <ToolBody tool={tool}/>
        </details>)}
      </details>
  )}</div>;
}
