import { describe, expect, it } from 'vitest';
import { artifactDownloadUrl, canPreviewArtifact, fileSize, pythonArtifactsFor } from './PythonArtifacts';
import type { Artifact, Message } from './types';

const message = { role: 'assistant', run_id: 7 } as Message;
const image = { id: 12, run_id: 7, kind: 'python_output', title: 'chart.png', mime_type: 'image/png', metadata: { size_bytes: 1024 } } as Artifact;

describe('Python artifacts in messages', () => {
  it('only selects Python outputs from the message run', () => {
    expect(pythonArtifactsFor(message, [image, { ...image, id: 13, run_id: 8 }, { ...image, id: 14, kind: 'data_output' }])).toEqual([image]);
  });

  it('only previews bounded PNG and JPEG outputs through the controlled endpoint', () => {
    expect(canPreviewArtifact(image)).toBe(true);
    expect(canPreviewArtifact({ ...image, metadata: { size_bytes: 8 * 1024 * 1024 + 1 } })).toBe(false);
    expect(canPreviewArtifact({ ...image, mime_type: 'image/svg+xml' })).toBe(false);
    expect(artifactDownloadUrl(12)).toBe('/api/v3/artifacts/12/download');
    expect(artifactDownloadUrl(-1)).toBeUndefined();
  });
});

describe('artifact file size labels', () => {
  it('shows bytes below 1 KB', () => {
    expect(fileSize(0)).toBe('0 B');
    expect(fileSize(512)).toBe('512 B');
    expect(fileSize(1023)).toBe('1023 B');
  });

  it('shows one-decimal KB below 1 MB', () => {
    expect(fileSize(1024)).toBe('1.0 KB');
    expect(fileSize(1536)).toBe('1.5 KB');
    expect(fileSize(1024 * 1024 - 1)).toBe('1024.0 KB');
  });

  it('shows one-decimal MB from 1 MB up and handles missing sizes', () => {
    expect(fileSize(1024 * 1024)).toBe('1.0 MB');
    expect(fileSize(3.2 * 1024 * 1024)).toBe('3.2 MB');
    expect(fileSize(undefined)).toBe('');
  });
});
