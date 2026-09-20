import { FileDown } from 'lucide-react';
import type { Artifact, Message } from './types';

const previewTypes = new Set(['image/png', 'image/jpeg']);
const maxPreviewSize = 8 * 1024 * 1024;

export function pythonArtifactsFor(message: Message, artifacts: Artifact[]) {
  return message.role === 'assistant' && message.run_id
    ? artifacts.filter((artifact) => artifact.run_id === message.run_id && artifact.kind === 'python_output')
    : [];
}

export function artifactDownloadUrl(id: number) {
  return Number.isInteger(id) && id > 0 ? `/api/v3/artifacts/${id}/download` : undefined;
}

export function canPreviewArtifact(artifact: Artifact) {
  return previewTypes.has(artifact.mime_type) && (artifact.metadata?.size_bytes ?? Infinity) <= maxPreviewSize;
}

export function fileSize(bytes?: number) {
  if (!bytes && bytes !== 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function PythonArtifacts({ message, artifacts }: { message: Message; artifacts: Artifact[] }) {
  const outputs = pythonArtifactsFor(message, artifacts);
  if (!outputs.length) return null;
  return <section className="python-artifacts" aria-label="Python 分析成果">
    <span className="python-artifacts-title">Python 成果</span>
    <div className="python-artifacts-list">
      {outputs.map((artifact) => {
        const url = artifactDownloadUrl(artifact.id);
        if (!url) return null;
        return <div className="python-artifact" key={artifact.id}>
          <a href={url} download>
            <FileDown size={13} />
            <span>{artifact.title}<small>{fileSize(artifact.metadata?.size_bytes)}</small></span>
          </a>
          {canPreviewArtifact(artifact) && <details>
            <summary className="section-label">预览图表</summary>
            <img src={url} alt={`${artifact.title} 图表预览`} loading="lazy" />
          </details>}
        </div>;
      })}
    </div>
  </section>;
}
