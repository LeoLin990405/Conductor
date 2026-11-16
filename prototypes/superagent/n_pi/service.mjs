// Prototype N: the Super-agent loop on pi-agent-core, served to a Python adapter.
//
//     node service.mjs <bridge_path> <scratch_dir>
//
// Control channel: stdin/stdout, one JSON object per line, from the adapter
// (`hello`, `start`, `turn`, `session`, `transcript`, `event`, `close`).
// Everything the loop needs (model, store, credit gate, memory, subscriber,
// tool handlers) is reached over the contract bridge on <bridge_path>.
//
// pi pieces in use:
// - pi-ai `createModels` + `createProvider`: a provider whose stream function
//   is the bridge's scripted model (`model.open` / `model.next`). This is the
//   seam a hosted provider (anthropic, openai, ...) occupies in production.
// - pi-agent-core `Agent`: the tool loop. `prepareRequest` asks the credit
//   gate before every model round and injects recalled memory before the
//   first one; tools are the role's autonomy tools only, each one executing
//   through bridge `tools.call`; `subscribe` publishes stream events.

import { writeFileSync } from "node:fs";
import path from "node:path";
import readline from "node:readline";

import { Agent } from "@earendil-works/pi-agent-core";
import {
  AssistantMessageEventStream,
  createModels,
  createProvider,
  getCurrentSystemPrompt,
  getCurrentTools,
} from "@earendil-works/pi-ai";

import { BridgeClient } from "./bridge-client.mjs";

// stdout is the control channel. Nothing else may write to it.
console.log = console.info = console.debug = console.warn = console.error;

const MIN_NODE = [22, 19];

const ZERO_USAGE = {
  input: 0,
  output: 0,
  cacheRead: 0,
  cacheWrite: 0,
  totalTokens: 0,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
};

const SCRIPTED_MODEL = {
  id: "scripted",
  name: "Scripted model over the contract bridge",
  api: "scripted-bridge",
  provider: "scripted",
  baseUrl: "",
  reasoning: false,
  input: ["text"],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  contextWindow: 1_000_000,
  maxTokens: 100_000,
};

// The scripted model sends free-form probe arguments. Production would use
// each tool's real JSON schema here.
const ANY_ARGUMENTS = { type: "object", additionalProperties: true };

function systemPrompt(role) {
  return `role=${role}\nRoute or review only. Do not run shell commands or edit files.`;
}

const text = (value) => ({ type: "text", text: value });
const userMessage = (value) => ({ role: "user", content: [text(value)], timestamp: 0 });

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((ok, fail) => {
    resolve = ok;
    reject = fail;
  });
  promise.catch(() => {});
  return { promise, resolve, reject };
}

// --- store transcript -> pi messages ------------------------------------------

function historyMessages(rows) {
  const out = [];
  let pendingCall = null;
  const appendAssistant = (block) => {
    const last = out.at(-1);
    const open =
      last?.role === "assistant" && !last.content.some((part) => part.type === "toolCall");
    if (open) {
      last.content.push(block);
    } else {
      out.push({
        role: "assistant",
        content: [block],
        api: SCRIPTED_MODEL.api,
        provider: SCRIPTED_MODEL.provider,
        model: SCRIPTED_MODEL.id,
        usage: ZERO_USAGE,
        stopReason: block.type === "toolCall" ? "toolUse" : "stop",
        timestamp: 0,
      });
    }
  };
  for (const row of rows) {
    switch (row.kind) {
      case "user":
      case "memory":
      case "tool_refused":
        out.push(userMessage(row.text));
        break;
      case "assistant":
        appendAssistant(text(row.text));
        break;
      case "tool_call": {
        const call = JSON.parse(row.text);
        pendingCall = { id: `hist_${row.seq}`, name: call.name };
        appendAssistant({
          type: "toolCall",
          id: pendingCall.id,
          name: call.name,
          arguments: call.arguments ?? {},
        });
        break;
      }
      case "tool_result":
        if (pendingCall) {
          out.push({
            role: "toolResult",
            toolCallId: pendingCall.id,
            toolName: pendingCall.name,
            content: [text(row.text)],
            isError: false,
            timestamp: 0,
          });
          pendingCall = null;
        }
        break;
      default:
        // `stop` and `done` are stream markers, not conversation.
        break;
    }
  }
  return out;
}

// --- pi transcript -> bridge request (Anthropic content blocks) ---------------

function contentBlocks(content) {
  if (typeof content === "string") return [text(content)];
  return content.filter((part) => part.type === "text").map((part) => text(part.text));
}

function bridgeMessages(messages) {
  const out = [{ role: "system", content: [text(getCurrentSystemPrompt(messages))] }];
  for (const message of messages) {
    if (message.role === "user") {
      out.push({ role: "user", content: contentBlocks(message.content) });
    } else if (message.role === "assistant") {
      if (message.stopReason === "error" || message.stopReason === "aborted") continue;
      const blocks = [];
      for (const part of message.content) {
        if (part.type === "text") blocks.push(text(part.text));
        if (part.type === "toolCall") {
          blocks.push({ type: "tool_use", id: part.id, name: part.name, input: part.arguments });
        }
      }
      out.push({ role: "assistant", content: blocks });
    } else if (message.role === "toolResult") {
      out.push({
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: message.toolCallId,
            content: contentBlocks(message.content),
          },
        ],
      });
    }
  }
  return out;
}

// --- one turn's rows and events -----------------------------------------------

class TurnState {
  constructor(bridge, session) {
    this.bridge = bridge;
    this.session = session;
    this.sessionId = session.session_id;
    this.original = Number(session.next_seq);
    this.seq = this.original;
    this.ops = [];
    this.published = [];
    this.stopped = false;
    this.error = null;
    this.tokenAck = null;
  }

  async take() {
    // Every seq comes from the bridge's shared allocator.
    const { first } = await this.bridge.call("seq.allocate", { session_id: this.sessionId });
    this.seq = first + 1;
    return first;
  }

  row(seq, kind, value) {
    return { session_id: this.sessionId, seq, kind, text: value };
  }

  async record(kind, value) {
    this.ops.push({ op: "append_transcript", row: this.row(await this.take(), kind, value) });
  }

  async emit(kind, value, transcriptKind = kind) {
    const seq = await this.take();
    this.ops.push({ op: "append_transcript", row: this.row(seq, transcriptKind, value) });
    this.ops.push({ op: "append_event", row: this.row(seq, kind, value) });
    const event = { seq, kind, text: value };
    await this.bridge.call("subscriber.publish", event);
    this.published.push(event);
  }

  expectToken() {
    this.tokenAck = deferred();
    return this.tokenAck.promise;
  }

  tokenPublished() {
    this.tokenAck?.resolve();
    this.tokenAck = null;
  }

  fail(error) {
    this.error ??= error;
    this.tokenAck?.reject(error);
    this.tokenAck = null;
  }

  async commit(status) {
    const row = { ...this.session, next_seq: this.seq, status };
    this.ops.push({ op: "upsert_session", row });
    await this.bridge.call("store.commit", {
      ops: this.ops,
      expect: { [this.sessionId]: this.original },
    });
    return this.published;
  }
}

// --- the service ----------------------------------------------------------------

class Service {
  constructor(bridge) {
    this.bridge = bridge;
    this.roleTools = {};
    this.current = null;
    this.calls = 0;
    this.models = createModels();
    this.models.setProvider(
      createProvider({
        id: SCRIPTED_MODEL.provider,
        name: SCRIPTED_MODEL.name,
        auth: { apiKey: { name: "none", resolve: async () => ({ auth: {} }) } },
        models: [SCRIPTED_MODEL],
        api: {
          stream: (model, context, options) => this.modelRound(model, context, options),
          streamSimple: (model, context, options) => this.modelRound(model, context, options),
        },
      }),
    );
  }

  hello(roleTools) {
    this.roleTools = roleTools;
  }

  // One model round: pi-ai's stream protocol fed by bridge model.open/next.
  modelRound(model, context, options) {
    const stream = new AssistantMessageEventStream();
    const turn = this.current;
    const partial = {
      role: "assistant",
      content: [],
      api: model.api,
      provider: model.provider,
      model: model.id,
      usage: ZERO_USAGE,
      stopReason: "stop",
      timestamp: Date.now(),
    };
    const produce = async () => {
      if (options?.signal?.aborted) {
        stream.push({
          type: "error",
          reason: "aborted",
          error: { ...partial, stopReason: "aborted", errorMessage: "stopped before the model" },
        });
        return;
      }
      const tools = getCurrentTools(context.messages).map((tool) => ({
        name: tool.name,
        description: tool.description,
        input_schema: tool.parameters,
      }));
      await this.bridge.call("model.open", { messages: bridgeMessages(context.messages), tools });
      stream.push({ type: "start", partial });
      let chunk;
      while ((chunk = await this.bridge.call("model.next")) !== null) {
        if (chunk.text) {
          const contentIndex = partial.content.length;
          partial.content.push(text(""));
          stream.push({ type: "text_start", contentIndex, partial });
          partial.content[contentIndex].text = chunk.text;
          stream.push({ type: "text_delta", contentIndex, delta: chunk.text, partial });
          // Lockstep: the next chunk is asked for only after this one is published.
          const published = turn.expectToken();
          stream.push({ type: "text_end", contentIndex, content: chunk.text, partial });
          await published;
        }
        if (chunk.tool_name) {
          const contentIndex = partial.content.length;
          const toolCall = {
            type: "toolCall",
            id: `call_${++this.calls}`,
            name: chunk.tool_name,
            arguments: chunk.tool_args ?? {},
          };
          partial.content.push(toolCall);
          stream.push({ type: "toolcall_start", contentIndex, partial });
          stream.push({
            type: "toolcall_delta",
            contentIndex,
            delta: JSON.stringify(toolCall.arguments),
            partial,
          });
          stream.push({ type: "toolcall_end", contentIndex, toolCall, partial });
        }
      }
      partial.stopReason = partial.content.some((part) => part.type === "toolCall")
        ? "toolUse"
        : "stop";
      stream.push({ type: "done", reason: partial.stopReason, message: partial });
    };
    produce().catch((error) => {
      turn?.fail(error);
      stream.push({
        type: "error",
        reason: "error",
        error: { ...partial, stopReason: "error", errorMessage: String(error?.message ?? error) },
      });
    });
    return stream;
  }

  async getSession(sessionId) {
    const row = await this.bridge.call("store.get_session", { session_id: sessionId });
    if (row === null) throw new Error(`session ${sessionId} is not started`);
    return row;
  }

  async start(sessionId, role, userId) {
    const existing = await this.bridge.call("store.get_session", { session_id: sessionId });
    if (existing !== null) return;
    const row = { session_id: sessionId, user_id: userId, role, status: "ready", next_seq: 1 };
    await this.bridge.call("store.commit", {
      ops: [{ op: "upsert_session", row }],
      expect: { [sessionId]: null },
    });
  }

  rows(kind, sessionId) {
    return this.bridge.call(`store.${kind}_rows`, { session_id: sessionId });
  }

  async turn(sessionId, userText) {
    const session = await this.getSession(sessionId);
    const { user_id: userId, role } = session;
    const definitions = this.roleTools[role];
    if (!definitions) throw new Error(`no tool list for role ${role}`);
    const names = new Set(definitions.map((tool) => tool.name));
    const history = await this.rows("transcript", sessionId);
    const bridge = this.bridge;
    const turn = new TurnState(bridge, session);
    this.current = turn;
    const guard =
      (fn) =>
      async (...args) => {
        try {
          return await fn(...args);
        } catch (error) {
          turn.fail(error);
          throw error;
        }
      };

    await turn.record("user", userText);

    const executed = new Map();
    const tools = definitions.map((definition) => ({
      name: definition.name,
      label: definition.name,
      description: definition.description,
      parameters: ANY_ARGUMENTS,
      executionMode: "sequential",
      execute: guard(async (toolCallId, params) => {
        const done = await bridge.call("tools.call", {
          session_id: sessionId,
          name: definition.name,
          arguments: params,
        });
        executed.set(toolCallId, done);
        return { content: [text(done.result)], details: { call: done.call } };
      }),
    }));

    let firstRequest = true;
    const agent = new Agent({
      initialState: {
        systemPrompt: systemPrompt(role),
        model: SCRIPTED_MODEL,
        thinkingLevel: "off",
        tools,
        messages: historyMessages(history),
      },
      streamFn: (model, context, options) => this.models.streamSimple(model, context, options),
      convertToLlm: (messages) => messages,
      toolExecution: "sequential",
      // Before every model round: the credit gate. Before the first: memory.
      prepareRequest: guard(async ({ context }) => {
        // pi turns a thrown tool error into an error result and keeps
        // looping; a failed turn is already lost (nothing will be committed),
        // so stop before asking for credit or calling the model again.
        if (turn.error) {
          agent.abort();
          return undefined;
        }
        const decision = await bridge.call("credit.authorize", {
          user_id: userId,
          session_id: sessionId,
        });
        if (!decision.allowed) {
          await turn.emit("stop", decision.reason);
          turn.stopped = true;
          agent.abort();
          return undefined;
        }
        if (!firstRequest) return undefined;
        firstRequest = false;
        const hits = await bridge.call("memory.recall", { user_id: userId, query: userText });
        const memoryText = hits.map((hit) => hit.text).join("\n");
        await turn.record("memory", memoryText);
        const messages = [...context.messages];
        let at = messages.length;
        while (at > 0 && messages[at - 1].role !== "user") at -= 1;
        messages.splice(Math.max(at - 1, 0), 0, userMessage(memoryText));
        return { context: { ...context, messages } };
      }),
      // Defense in depth: only the role's tools are registered, so any other
      // name is already "not found" before this hook; refuse it here too.
      beforeToolCall: async ({ toolCall }) =>
        names.has(toolCall.name)
          ? undefined
          : { block: true, reason: `refused: ${toolCall.name}` },
    });

    agent.subscribe(
      guard(async (event) => {
        if (event.type === "message_update" && event.assistantMessageEvent.type === "text_end") {
          await turn.emit("token", event.assistantMessageEvent.content, "assistant");
          turn.tokenPublished();
        } else if (event.type === "tool_execution_end") {
          const done = executed.get(event.toolCallId);
          if (done) {
            await turn.emit("tool_call", done.call);
            await turn.emit("tool_result", done.result);
          } else {
            await turn.emit("tool_refused", event.toolName);
          }
        }
      }),
    );

    try {
      await agent.prompt(userText);
    } finally {
      this.current = null;
    }
    if (turn.error) throw turn.error;
    if (!turn.stopped) {
      if (agent.state.errorMessage) throw new Error(agent.state.errorMessage);
      await turn.emit("done", "");
    }
    return turn.commit(turn.stopped ? "stopped" : "ready");
  }
}

// --- process ----------------------------------------------------------------------

function checkNode() {
  const [major, minor] = process.versions.node.split(".").map(Number);
  if (major < MIN_NODE[0] || (major === MIN_NODE[0] && minor < MIN_NODE[1])) {
    throw new Error(`node ${process.versions.node} < ${MIN_NODE.join(".")} (pi 1.5.2 engines)`);
  }
}

async function dispatch(service, request) {
  switch (request.op) {
    case "hello":
      return service.hello(request.role_tools);
    case "start":
      return service.start(request.session_id, request.role, request.user_id);
    case "turn":
      return service.turn(request.session_id, request.user_text);
    case "session":
      return service.getSession(request.session_id);
    case "transcript":
    case "event":
      return service.rows(request.op, request.session_id);
    default:
      throw new Error(`unknown op ${request.op}`);
  }
}

async function main() {
  checkNode();
  const [bridgePath, scratch] = process.argv.slice(2);
  if (!bridgePath || !scratch) throw new Error("usage: node service.mjs <bridge_path> <scratch_dir>");
  writeFileSync(path.join(scratch, "runtime.pid"), String(process.pid));
  let closing = false;
  const bridge = new BridgeClient(bridgePath, {
    onClose: (error) => {
      if (closing) return;
      console.error(`n_pi: ${error.message}; exiting`);
      process.exit(3);
    },
  });
  const service = new Service(bridge);
  const ready = bridge.connect();
  const reply = (body) => process.stdout.write(`${JSON.stringify(body)}\n`);
  const shutdown = (code) => {
    closing = true;
    bridge.close();
    process.stdout.write("", () => process.exit(code));
  };
  ready.catch((error) => {
    console.error(`n_pi: cannot reach the bridge: ${error.message}`);
    process.exit(2);
  });

  // Requests are answered strictly in order, one at a time.
  let queue = ready;
  const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  lines.on("line", (line) => {
    if (!line.trim()) return;
    queue = queue.then(async () => {
      if (closing) return;
      const request = JSON.parse(line);
      if (request.op === "close") {
        reply({ ok: true, result: null });
        shutdown(0);
        return;
      }
      try {
        const result = await dispatch(service, request);
        reply({ ok: true, result: result ?? null });
      } catch (error) {
        reply({ ok: false, error: `${error?.name ?? "Error"}: ${error?.message ?? error}` });
      }
    });
  });
  // EOF on the control channel ends the service.
  lines.on("close", () => {
    queue = queue.then(() => shutdown(0));
  });
}

main().catch((error) => {
  console.error(`n_pi: ${error?.stack ?? error}`);
  process.exit(1);
});
