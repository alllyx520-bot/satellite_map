import { describe, expect, it } from 'vitest';
import { groupTools, runtimeLabel, toolName, toolRuntime, toolStatus } from './ToolActivity';
import { statFields } from './DataEvidence';

describe('tool activity labels', () => {
  it('uses a friendly label and preserves the Python runtime', () => {
    const tool = { name: 'python_analysis', result: { runtime: 'local' } };
    expect(toolName(tool.name)).toBe('Python 分析');
    expect(toolRuntime(tool)).toBe('local');
  });

  it('maps known tool names to Chinese labels', () => {
    expect(toolName('read_image_window')).toBe('查看原图窗口');
    expect(toolName('search_scenes')).toBe('检索影像场景');
    expect(toolName('compare_two_date_change')).toBe('两期变化分析');
    expect(toolName('finish_answer')).toBe('生成回答');
  });

  it('falls back to spaced lowercase for unknown tools and hides their runtime', () => {
    const tool = { name: 'some_new_tool', result: { runtime: 'local' } };
    expect(toolName(tool.name)).toBe('some new tool');
    expect(toolRuntime(tool)).toBeUndefined();
    expect(runtimeLabel('local')).toBe('本地执行');
  });
});

describe('tool status semantics', () => {
  it('prefers the adjust state when the result carries an error', () => {
    expect(toolStatus({ status: 'completed', result: { error: 'boom' } })).toMatchObject({ key: 'adjust', cls: 'st-adjust' });
  });

  it('maps statuses to label and style classes', () => {
    expect(toolStatus({ status: 'completed' })).toMatchObject({ label: '完成', cls: 'st-done', dot: 'completed' });
    expect(toolStatus({ status: 'uncertain' })).toMatchObject({ label: '结果未知', cls: 'st-unknown' });
    expect(toolStatus({ status: 'running' })).toMatchObject({ label: '执行中', cls: 'st-running', dot: 'running' });
    expect(toolStatus({ status: 'error' })).toMatchObject({ label: '错误', cls: 'st-error', dot: 'error' });
  });
});

describe('tool grouping', () => {
  const tool = (id: number, name: string, status: string) => ({ id, name, status });

  it('folds consecutive tools with the same name and status', () => {
    const groups = groupTools([
      tool(1, 'search_scenes', 'completed'),
      tool(2, 'search_scenes', 'completed'),
      tool(3, 'search_scenes', 'completed'),
      tool(4, 'retrieve_imagery', 'completed'),
    ]);
    expect(groups).toHaveLength(2);
    expect(groups[0].items.map((t) => t.id)).toEqual([1, 2, 3]);
    expect(groups[1].items.map((t) => t.id)).toEqual([4]);
  });

  it('does not fold across status or name changes', () => {
    const groups = groupTools([
      tool(1, 'search_scenes', 'completed'),
      tool(2, 'search_scenes', 'running'),
      tool(3, 'search_scenes', 'running'),
      tool(4, 'compute_product', 'running'),
      tool(5, 'search_scenes', 'running'),
    ]);
    expect(groups.map((g) => g.items.length)).toEqual([1, 2, 1, 1]);
  });

  it('treats result errors as the adjust state when grouping', () => {
    const groups = groupTools([
      { id: 1, name: 'search_scenes', status: 'completed', result: { error: 'x' } },
      { id: 2, name: 'search_scenes', status: 'completed', result: { error: 'y' } },
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].status).toBe('adjust');
  });
});

describe('data evidence stat fields', () => {
  it('splits units out of the label and keeps unitless fields plain', () => {
    const fields = statFields('dem_terrain', { mean_elevation_m: 512.34, mean_slope_deg: 12, ignored: 1, mean: 'x' });
    expect(fields).toEqual([
      { key: 'mean_elevation_m', label: '平均高程', unit: 'm', value: 512.34, highlight: false },
      { key: 'mean_slope_deg', label: '平均坡度', unit: '°', value: 12, highlight: false },
    ]);
  });

  it('has no unit for index means and highlights strong index means', () => {
    const [field] = statFields('ndvi', { mean: 0.72 });
    expect(field).toMatchObject({ label: '均值', unit: undefined, highlight: true });
    expect(statFields('ndvi', { mean: 0.4 })[0].highlight).toBe(false);
    expect(statFields('weather', { mean: 0.9 })[0].highlight).toBe(false);
  });

  it('highlights positive fire counts', () => {
    expect(statFields('fire_detections', { fire_count: 3 })[0]).toMatchObject({ unit: '个', highlight: true });
    expect(statFields('fire_detections', { fire_count: 0 })[0].highlight).toBe(false);
  });
});
