// JSON-lines client for the contract bridge (protocol v2), over a unix socket.
//
// One connection, requests answered in order. Calls may be issued while
// others are in flight; replies are matched by id. A closed socket rejects
// every pending call and every later one: the service treats that as fatal.

import net from "node:net";

export const PROTOCOL_VERSION = 2;

export class BridgeError extends Error {}

export class BridgeClient {
  #socket;
  #buffer = "";
  #nextId = 0;
  #pending = new Map();
  #closed = false;
  #onClose;

  constructor(path, { onClose } = {}) {
    this.path = path;
    this.#onClose = onClose;
  }

  async connect() {
    await new Promise((resolve, reject) => {
      this.#socket = net.connect({ path: this.path }, resolve);
      this.#socket.once("error", reject);
    });
    this.#socket.setEncoding("utf8");
    this.#socket.on("data", (chunk) => this.#receive(chunk));
    this.#socket.on("error", () => {});
    this.#socket.on("close", () => this.#fail(new BridgeError("bridge closed the connection")));
    const hello = await this.call("hello", { protocol: PROTOCOL_VERSION });
    if (hello?.protocol !== PROTOCOL_VERSION) {
      throw new BridgeError(`bridge answered protocol ${hello?.protocol}`);
    }
    return this;
  }

  call(method, params = {}) {
    if (this.#closed) {
      return Promise.reject(new BridgeError("bridge is closed"));
    }
    const id = ++this.#nextId;
    return new Promise((resolve, reject) => {
      this.#pending.set(id, { resolve, reject, method });
      this.#socket.write(`${JSON.stringify({ id, method, params })}\n`);
    });
  }

  close() {
    this.#closed = true;
    this.#socket?.end();
    this.#socket?.destroy();
  }

  #receive(chunk) {
    this.#buffer += chunk;
    let newline;
    while ((newline = this.#buffer.indexOf("\n")) >= 0) {
      const line = this.#buffer.slice(0, newline);
      this.#buffer = this.#buffer.slice(newline + 1);
      if (!line.trim()) continue;
      const reply = JSON.parse(line);
      const waiter = this.#pending.get(reply.id);
      if (!waiter) {
        this.#fail(new BridgeError(`bridge reply out of order: ${line}`));
        return;
      }
      this.#pending.delete(reply.id);
      if (reply.error) {
        waiter.reject(
          new BridgeError(`${waiter.method}: ${reply.error.type}: ${reply.error.message}`),
        );
      } else {
        waiter.resolve(reply.result ?? null);
      }
    }
  }

  #fail(error) {
    const wasOpen = !this.#closed;
    this.#closed = true;
    for (const waiter of this.#pending.values()) waiter.reject(error);
    this.#pending.clear();
    if (wasOpen) this.#onClose?.(error);
  }
}
