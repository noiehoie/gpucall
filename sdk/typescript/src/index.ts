export type DataRef = {
  uri: string;
  sha256?: string;
  bytes?: number;
  expires_at?: string;
  content_type?: string;
};

declare const process: { env: Record<string, string | undefined> } | undefined;

export type TaskResult = Record<string, unknown>;
export type ResponseFormat =
  | { type: "text" }
  | { type: "json_object"; strict?: boolean }
  | { type: "json_schema"; json_schema: Record<string, unknown>; strict?: boolean };
export type ChatMessage = { role: "system" | "user" | "assistant" | "tool"; content: string };

export class GPUCallWarning extends Error {}

function normalizeMessages(messages: ChatMessage[]): ChatMessage[] {
  return messages.map((message, index) => {
    if (typeof message.content !== "string") {
      throw new TypeError(`messages[${index}].content must be a string; use gpucall DataRef uploads for files or multimodal input`);
    }
    return message;
  });
}

export class GPUCallClient {
  constructor(
    private readonly baseUrl: string,
    private readonly apiKey?: string,
  ) {}

  static fromEnv(baseUrl: string): GPUCallClient {
    const envKey = typeof process !== "undefined" ? process.env.GPUCALL_API_KEY : undefined;
    return new GPUCallClient(baseUrl, envKey);
  }

  async infer(options: {
    prompt?: string;
    messages?: ChatMessage[];
    files?: File[];
    mode?: "sync" | "async" | "stream";
    task?: "infer" | "vision";
    responseFormat?: ResponseFormat;
    maxTokens?: number;
    temperature?: number;
    poll?: boolean;
  }): Promise<TaskResult> {
    const mode = options.mode ?? "sync";
    const refs = await Promise.all((options.files ?? []).map((file) => this.uploadFile(file)));
    const payload: Record<string, unknown> = {
      task: options.task ?? "infer",
      mode,
      input_refs: refs,
      inline_inputs: options.prompt ? { prompt: { value: options.prompt } } : {},
    };
    if (options.messages) payload.messages = normalizeMessages(options.messages);
    if (options.responseFormat) payload.response_format = options.responseFormat;
    if (options.maxTokens !== undefined) payload.max_tokens = options.maxTokens;
    if (options.temperature !== undefined) payload.temperature = options.temperature;
    const response = await this.request(`/v2/tasks/${mode}`, {
      method: "POST",
      body: JSON.stringify(payload),
      headers: { "content-type": "application/json" },
    });
    const data = await response.json();
    if (response.status === 202 && options.poll !== false) {
      return this.pollJob(String(data.job_id));
    }
    return data;
  }

  async vision(options: { image: File; prompt?: string; mode?: "sync" | "async"; responseFormat?: ResponseFormat }): Promise<TaskResult> {
    return this.infer({
      prompt: options.prompt,
      files: [options.image],
      mode: options.mode,
      task: "vision",
      responseFormat: options.responseFormat,
    });
  }

  async stream(options: { prompt?: string; messages?: ChatMessage[]; files?: File[]; task?: "infer" | "vision"; responseFormat?: ResponseFormat }): Promise<ReadableStream<Uint8Array> | null> {
    const refs = await Promise.all((options.files ?? []).map((file) => this.uploadFile(file)));
    const payload: Record<string, unknown> = {
      task: options.task ?? "infer",
      mode: "stream",
      input_refs: refs,
      inline_inputs: options.prompt ? { prompt: { value: options.prompt } } : {},
    };
    if (options.messages) payload.messages = normalizeMessages(options.messages);
    if (options.responseFormat) payload.response_format = options.responseFormat;
    const response = await this.request("/v2/tasks/stream", {
      method: "POST",
      body: JSON.stringify(payload),
      headers: { "content-type": "application/json" },
    });
    return response.body;
  }

  async uploadFile(file: File): Promise<DataRef> {
    const buffer = await file.arrayBuffer();
    const hash = await crypto.subtle.digest("SHA-256", buffer);
    const sha256 = Array.from(new Uint8Array(hash)).map((b) => b.toString(16).padStart(2, "0")).join("");
    const presign = await this.request("/v2/objects/presign-put", {
      method: "POST",
      body: JSON.stringify({ name: file.name, bytes: file.size, sha256, content_type: file.type || "application/octet-stream" }),
      headers: { "content-type": "application/json" },
    }).then((r) => r.json());
    const upload = await fetch(presign.upload_url, { method: "PUT", body: file, headers: { "content-type": file.type || "application/octet-stream" } });
    if (!upload.ok) throw new Error(`upload failed: ${upload.status}`);
    return presign.data_ref;
  }

  async pollJob(jobId: string, intervalMs = 1000, timeoutMs = 300000): Promise<TaskResult> {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const response = await this.request(`/v2/jobs/${jobId}`);
      const job = await response.json();
      if (["COMPLETED", "FAILED", "CANCELLED", "EXPIRED"].includes(String(job.state))) return job;
      await new Promise((resolve) => setTimeout(resolve, intervalMs));
    }
    throw new Error(`job ${jobId} did not finish within ${timeoutMs}ms`);
  }

  private async request(path: string, init: RequestInit = {}): Promise<Response> {
    const headers = new Headers(init.headers);
    if (this.apiKey) headers.set("authorization", `Bearer ${this.apiKey}`);
    const response = await fetch(`${this.baseUrl.replace(/\/$/, "")}${path}`, { ...init, headers });
    const warning = response.headers.get("x-gpucall-warning");
    if (warning) console.warn(new GPUCallWarning(warning));
    if (!response.ok) {
      let detail = `gpucall request failed: ${response.status}`;
      try {
        const body = await response.clone().json();
        if (body?.detail) detail = String(body.detail);
      } catch {
        // keep status-only detail
      }
      throw new Error(detail);
    }
    return response;
  }
}
