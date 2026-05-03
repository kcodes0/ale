import { chmod, mkdir, rm, readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import { Poke } from "poke";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js";
import * as z from "zod/v4";

type TaskMode = "investigate" | "fix" | "review" | "deploy";
type Priority = "low" | "normal" | "high";
type JobStatus = "queued" | "awaiting_approval" | "running" | "completed" | "failed" | "cancelled";

type Job = {
  id: string;
  repo: string;
  task: string;
  mode: TaskMode;
  priority: Priority;
  status: JobStatus;
  createdAt: string;
  updatedAt: string;
  startedAt?: string;
  finishedAt?: string;
  workspace?: string;
  exitCode?: number;
  error?: string;
  result?: string;
  approvalRequired?: boolean;
  approvalReason?: string;
  process?: Bun.Subprocess;
  controller?: AbortController;
};

const config = {
  port: Number(process.env.PORT ?? 3000),
  publicBaseUrl: process.env.PUBLIC_BASE_URL ?? `http://localhost:${process.env.PORT ?? 3000}`,
  apiKey: process.env.PI_CLOUD_API_KEY,
  requireMcpAuth: process.env.PI_CLOUD_REQUIRE_MCP_AUTH !== "false",
  workspacesDir: process.env.WORKSPACES_DIR ?? path.join(process.cwd(), ".pi-cloud", "workspaces"),
  logsDir: process.env.LOGS_DIR ?? path.join(process.cwd(), ".pi-cloud", "logs"),
  maxConcurrentJobs: Number(process.env.MAX_CONCURRENT_JOBS ?? 1),
  maxRuntimeMs: Number(process.env.MAX_RUNTIME_MS ?? 30 * 60 * 1000),
  useDocker: process.env.PI_CLOUD_USE_DOCKER !== "false",
  allowUnsandboxed: process.env.PI_CLOUD_ALLOW_UNSANDBOXED === "true",
  dockerImage: process.env.PI_AGENT_IMAGE ?? "pi-agent:latest",
  runnerCommand: process.env.PI_RUNNER_COMMAND ?? "pi -p \"$PI_TASK_PROMPT\"",
  piAgentHome: process.env.PI_AGENT_HOME,
  pokeApiKey: process.env.POKE_API_KEY,
  githubToken: process.env.GITHUB_TOKEN,
  allowedRepos: parseAllowedRepos(process.env.ALLOWED_REPOS ?? ""),
};

const jobs = new Map<string, Job>();
let activeJobs = 0;

function parseAllowedRepos(raw: string): Map<string, string> {
  const repos = new Map<string, string>();
  for (const part of raw.split(",").map((s) => s.trim()).filter(Boolean)) {
    const eq = part.indexOf("=");
    const name = eq >= 0 ? part.slice(0, eq).trim() : part;
    const url = eq >= 0 ? part.slice(eq + 1).trim() : `https://github.com/${part}.git`;
    repos.set(name, url);
  }
  return repos;
}

function requireAuth(req: Request): Response | undefined {
  if (!config.apiKey) return;
  const auth = req.headers.get("authorization");
  const bearer = auth?.startsWith("Bearer ") ? auth.slice("Bearer ".length) : undefined;
  if (bearer !== config.apiKey) return json({ error: "unauthorized" }, 401);
}

function json(value: unknown, status = 200) {
  return Response.json(value, { status, headers: corsHeaders() });
}

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type,Authorization,mcp-session-id,Last-Event-ID,mcp-protocol-version",
    "Access-Control-Expose-Headers": "mcp-session-id,mcp-protocol-version",
  };
}

function publicJob(job: Job) {
  const { process: _p, controller: _c, ...safe } = job;
  return safe;
}

function logPath(jobId: string) {
  return path.join(config.logsDir, `${jobId}.log`);
}

async function appendLog(job: Job, line: string) {
  await mkdir(config.logsDir, { recursive: true });
  const stamped = `[${new Date().toISOString()}] ${line}\n`;
  await Bun.write(logPath(job.id), (existsSync(logPath(job.id)) ? await readFile(logPath(job.id), "utf8") : "") + stamped);
}

function requiresApproval(mode: TaskMode, task: string) {
  const risky = /\b(prod|production|deploy|destroy|delete|drop|truncate|secret|credential|token|terraform apply|kubectl delete|rm -rf)\b/i;
  if (mode === "deploy") return "deploy mode requires explicit approval";
  if (risky.test(task)) return "task appears to include deploy, destructive, or secret-related actions";
}

async function startPiTask(input: { repo: string; task: string; mode?: TaskMode; priority?: Priority }) {
  const repoUrl = config.allowedRepos.get(input.repo);
  if (!repoUrl) throw new Error(`repo '${input.repo}' is not in ALLOWED_REPOS`);
  if (!input.task || input.task.length < 5) throw new Error("task is required");
  if (input.task.length > 8000) throw new Error("task is too long");

  const job: Job = {
    id: `job_${crypto.randomUUID().replaceAll("-", "").slice(0, 16)}`,
    repo: input.repo,
    task: input.task,
    mode: input.mode ?? "fix",
    priority: input.priority ?? "normal",
    status: "queued",
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  };

  const approvalReason = requiresApproval(job.mode, job.task);
  if (approvalReason) {
    job.status = "awaiting_approval";
    job.approvalRequired = true;
    job.approvalReason = approvalReason;
  }

  jobs.set(job.id, job);
  await appendLog(job, `created for ${job.repo} (${job.mode}/${job.priority})`);
  if (job.status === "queued") queueMicrotask(processQueue);
  return publicJob(job);
}

function processQueue() {
  if (activeJobs >= config.maxConcurrentJobs) return;
  const next = [...jobs.values()]
    .filter((j) => j.status === "queued")
    .sort((a, b) => priorityValue(b.priority) - priorityValue(a.priority) || a.createdAt.localeCompare(b.createdAt))[0];
  if (!next) return;
  activeJobs++;
  void runJob(next).finally(() => {
    activeJobs--;
    processQueue();
  });
}

function priorityValue(p: Priority) {
  return p === "high" ? 2 : p === "normal" ? 1 : 0;
}

async function runJob(job: Job) {
  const repoUrl = config.allowedRepos.get(job.repo)!;
  job.status = "running";
  job.startedAt = job.updatedAt = new Date().toISOString();
  job.workspace = path.join(config.workspacesDir, job.id);
  job.controller = new AbortController();
  await appendLog(job, "starting workspace");

  const timeout = setTimeout(() => {
    void appendLog(job, `max runtime exceeded (${config.maxRuntimeMs}ms); cancelling`);
    void cancelPiTask(job.id);
  }, config.maxRuntimeMs);

  try {
    await rm(job.workspace, { recursive: true, force: true });
    await mkdir(job.workspace, { recursive: true });
    await chmod(job.workspace, 0o777);
    await runCommand(job, "git", ["clone", "--depth", "1", repoUrl, "."], job.workspace);

    const prompt = buildPiPrompt(job);
    if (config.useDocker) await runDockerPi(job, prompt);
    else {
      if (!config.allowUnsandboxed) throw new Error("Docker sandbox is enabled but unavailable/configured off. Set PI_CLOUD_USE_DOCKER=true with PI_AGENT_IMAGE, or PI_CLOUD_ALLOW_UNSANDBOXED=true for local development only.");
      await runCommand(job, "sh", ["-lc", config.runnerCommand], job.workspace, { PI_TASK_PROMPT: prompt });
    }

    const log = await getLogs(job.id, 4000);
    job.status = "completed";
    job.result = summarizeLog(log);
    await appendLog(job, "completed");
    await notifyPoke(`Pi job completed for ${job.repo}: ${job.result}\nJob: ${job.id}`, job);
  } catch (err) {
    if ((job.status as JobStatus) !== "cancelled") {
      job.status = "failed";
      job.error = err instanceof Error ? err.message : String(err);
      await appendLog(job, `failed: ${job.error}`);
      await notifyPoke(`Pi job failed for ${job.repo}: ${job.error}\nJob: ${job.id}`, job);
    }
  } finally {
    clearTimeout(timeout);
    job.finishedAt = job.updatedAt = new Date().toISOString();
    job.process = undefined;
    job.controller = undefined;
  }
}

function buildPiPrompt(job: Job) {
  return `You are running in a controlled Pi Cloud Delegation job.\n\nRepo: ${job.repo}\nMode: ${job.mode}\nJob: ${job.id}\n\nTask:\n${job.task}\n\nRules:\n- Work only inside the checked-out repository.\n- Prefer opening a GitHub PR over direct pushes to protected branches.\n- Do not access or print secrets.\n- Do not run production deploys or destructive infra commands unless explicitly approved in the job instructions.\n- Run relevant tests and provide a concise final summary with changed files and PR URL if created.`;
}

async function runDockerPi(job: Job, prompt: string) {
  const name = `pi-cloud-${job.id}`;
  await runCommand(job, "docker", [
    "run", "--rm", "--name", name,
    "-v", `${job.workspace}:/workspace`,
    "-w", "/workspace",
    "-e", "PI_TASK_PROMPT",
    "-e", "HOME=/home/pi",
    ...(config.githubToken ? ["-e", "GITHUB_TOKEN"] : []),
    ...(config.piAgentHome ? ["-v", `${path.resolve(config.piAgentHome)}:/home/pi/.pi`] : []),
    config.dockerImage,
    "sh", "-lc", config.runnerCommand,
  ], job.workspace!, { PI_TASK_PROMPT: prompt, ...(config.githubToken ? { GITHUB_TOKEN: config.githubToken } : {}) });
}

async function runCommand(job: Job, cmd: string, args: string[], cwd: string, env: Record<string, string> = {}) {
  await appendLog(job, `$ ${cmd} ${args.map((a) => a.includes(" ") ? JSON.stringify(a) : a).join(" ")}`.replaceAll(config.githubToken ?? "__NO_TOKEN__", "***"));
  const proc = Bun.spawn([cmd, ...args], {
    cwd,
    env: { ...process.env, ...env },
    stdout: "pipe",
    stderr: "pipe",
    signal: job.controller?.signal,
  });
  job.process = proc;
  await Promise.all([pipeToLog(job, proc.stdout), pipeToLog(job, proc.stderr)]);
  const code = await proc.exited;
  job.exitCode = code;
  if (code !== 0) throw new Error(`${cmd} exited with code ${code}`);
}

async function pipeToLog(job: Job, stream: ReadableStream<Uint8Array>) {
  const reader = (stream as ReadableStream<Uint8Array>).pipeThrough(new TextDecoderStream() as unknown as TransformStream<Uint8Array, string>).getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (value) await appendLog(job, value.replaceAll(config.githubToken ?? "__NO_TOKEN__", "***").trimEnd());
  }
}

async function cancelPiTask(jobId: string) {
  const job = jobs.get(jobId);
  if (!job) throw new Error("job not found");
  if (["completed", "failed", "cancelled"].includes(job.status)) return publicJob(job);
  job.status = "cancelled";
  job.updatedAt = job.finishedAt = new Date().toISOString();
  job.controller?.abort();
  job.process?.kill();
  await appendLog(job, "cancelled");
  return publicJob(job);
}

async function approvePiTaskAction(jobId: string, action = "start", approved = true) {
  const job = jobs.get(jobId);
  if (!job) throw new Error("job not found");
  if (job.status !== "awaiting_approval") throw new Error("job is not awaiting approval");
  if (!approved) return cancelPiTask(jobId);
  if (action !== "start") throw new Error("only action='start' is supported in the MVP");
  job.status = "queued";
  job.approvalRequired = false;
  job.updatedAt = new Date().toISOString();
  await appendLog(job, "approved and queued");
  queueMicrotask(processQueue);
  return publicJob(job);
}

async function getLogs(jobId: string, maxChars = 12000) {
  const file = logPath(jobId);
  if (!existsSync(file)) return "";
  const text = await readFile(file, "utf8");
  return text.length > maxChars ? text.slice(-maxChars) : text;
}

function summarizeLog(log: string) {
  const lines = log.split("\n").filter(Boolean);
  return lines.slice(-12).join("\n").slice(0, 2000) || "completed; no log output";
}

async function notifyPoke(message: string, job?: Job) {
  if (!config.pokeApiKey) return;
  try {
    const baseUrl = process.env.POKE_API ?? "https://poke.com/api/v1";
    const res = await fetch(`${baseUrl.replace(/\/$/, "")}/inbound/api-message`, {
      method: "POST",
      headers: { Authorization: `Bearer ${config.pokeApiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify({ message, source: "pi-cloud", jobId: job?.id, repo: job?.repo }),
    });
    if (!res.ok) throw new Error(`Poke inbound API returned ${res.status}`);
  } catch (err) {
    try {
      const poke = new Poke({ apiKey: config.pokeApiKey });
      await poke.sendMessage(message);
    } catch {
      console.error("failed to notify Poke", err);
    }
  }
}

function mcpServer() {
  const server = new McpServer({ name: "pi-cloud-delegation", version: "0.1.0" });
  server.registerTool("start_pi_task", {
    title: "Start pi task",
    description: "Start a safe, sandboxed pi coding/cloud task for an allowlisted repository.",
    inputSchema: {
      repo: z.string(),
      task: z.string(),
      mode: z.enum(["investigate", "fix", "review", "deploy"]).optional(),
      priority: z.enum(["low", "normal", "high"]).optional(),
    },
  }, async (args) => textResult(await startPiTask(args)));
  server.registerTool("get_pi_task_status", { inputSchema: { jobId: z.string() } }, async ({ jobId }) => textResult(publicJob(mustJob(jobId))));
  server.registerTool("cancel_pi_task", { inputSchema: { jobId: z.string() } }, async ({ jobId }) => textResult(await cancelPiTask(jobId)));
  server.registerTool("list_pi_tasks", { inputSchema: {} }, async () => textResult([...jobs.values()].map(publicJob)));
  server.registerTool("get_pi_task_logs", { inputSchema: { jobId: z.string(), maxChars: z.number().optional() } }, async ({ jobId, maxChars }) => ({ content: [{ type: "text", text: await getLogs(jobId, maxChars) }] }));
  server.registerTool("approve_pi_task_action", { inputSchema: { jobId: z.string(), action: z.string().optional(), approved: z.boolean().optional() } }, async ({ jobId, action, approved }) => textResult(await approvePiTaskAction(jobId, action, approved ?? true)));
  return server;
}

function textResult(value: unknown) {
  return { content: [{ type: "text" as const, text: typeof value === "string" ? value : JSON.stringify(value, null, 2) }] };
}

function mustJob(jobId: string) {
  const job = jobs.get(jobId);
  if (!job) throw new Error("job not found");
  return job;
}

async function handleHttp(req: Request) {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: corsHeaders() });
  const url = new URL(req.url);
  if (url.pathname === "/health") return json({ ok: true, jobs: jobs.size, activeJobs });
  if (url.pathname === "/mcp") {
    if (config.requireMcpAuth) {
      const auth = requireAuth(req); if (auth) return auth;
    }
    const transport = new WebStandardStreamableHTTPServerTransport({ enableJsonResponse: true });
    const server = mcpServer();
    await server.connect(transport);
    return transport.handleRequest(req);
  }

  const auth = requireAuth(req); if (auth) return auth;
  try {
    if (url.pathname === "/api/tasks" && req.method === "POST") return json(await startPiTask(await req.json() as Parameters<typeof startPiTask>[0]));
    if (url.pathname === "/api/tasks" && req.method === "GET") return json([...jobs.values()].map(publicJob));
    const match = url.pathname.match(/^\/api\/tasks\/([^/]+)(?:\/(logs|cancel|approve))?$/);
    if (match) {
      const jobId = match[1]!;
      const action = match[2];
      if (!action && req.method === "GET") return json(publicJob(mustJob(jobId)));
      if (action === "logs" && req.method === "GET") return new Response(await getLogs(jobId), { headers: { "Content-Type": "text/plain", ...corsHeaders() } });
      if (action === "cancel" && req.method === "POST") return json(await cancelPiTask(jobId));
      if (action === "approve" && req.method === "POST") return json(await approvePiTaskAction(jobId, "start", true));
    }
    return json({ error: "not found" }, 404);
  } catch (err) {
    return json({ error: err instanceof Error ? err.message : String(err) }, 400);
  }
}

await mkdir(config.workspacesDir, { recursive: true });
await mkdir(config.logsDir, { recursive: true });

Bun.serve({ port: config.port, fetch: handleHttp });
console.log(`Pi Cloud Delegation Service listening on ${config.publicBaseUrl}`);
console.log(`MCP endpoint: ${config.publicBaseUrl}/mcp`);
if (!config.allowedRepos.size) console.warn("No ALLOWED_REPOS configured; start_pi_task will reject all repos.");
