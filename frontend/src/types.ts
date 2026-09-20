export type Attachment = {
  id: string;
  name: string;
  status: string;
  kind: string;
  coordinate_space: "image_pixels" | "geographic";
  width: number;
  height: number;
  bbox?: number[] | Record<string, number> | null;
  geometry?: unknown;
  crs?: string;
  transform?: number[];
  metadata: Record<string, any>;
  preview_url?: string | null;
  tile_url?: string | null;
  scene_id?: number;
  error?: string | null;
};
export type Observation = {
  id: string;
  attachment_id: string;
  label: string;
  kind: string;
  geometry?: unknown;
  window: number[];
  summary: string;
  confidence?: number | null;
  evidence_refs: string[];
  preview_url?: string | null;
  metadata?: Record<string, any>;
};
export type Message = {
  id: string;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  parts: any[];
  attachment_ids?: string[];
  status: string;
  delivery: string;
  run_id?: number;
  sequence: number;
  created_at: string;
};
export type Run = {
  id: number;
  status: string;
  goal: string;
  model: string;
  current_action: string;
  public_update?: string;
  plan: any[];
  budget: Record<string, number>;
  usage: Record<string, number>;
  error: string;
};
export type ExecutionCapability = {
  available: boolean;
  runtime: string;
  isolated: boolean;
  label: string;
  python_version?: string;
  packages?: Record<string, string | null>;
  network?: boolean;
};
export type Capabilities = {
  execution?: ExecutionCapability;
  sandbox?: ExecutionCapability;
};
export type Conversation = {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  active_run?: Run | null;
  event_sequence: number;
  starred?: boolean;
};
export type Detail = {
  conversation: Conversation;
  messages: Message[];
  attachments: Attachment[];
  observations: Observation[];
  runs: Run[];
  artifacts: Artifact[];
  evidence?: DataEvidence[];
};

export type Artifact = {
  id: number;
  run_id: number;
  kind: string;
  title: string;
  mime_type: string;
  download_url: string;
  metadata?: { size_bytes?: number };
};

export type DataEvidence = {id:string; run_id:number; kind:string; metric:string; value:Record<string,any>; method:string;
  bbox?:Record<string,number>|null; data_contract:Record<string,any>; limitations:string[]; confidence?:number|null};
