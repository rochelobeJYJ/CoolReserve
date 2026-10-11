// SPDX-License-Identifier: GPL-3.0-only
// Additional terms: see LICENSE-ADDITIONAL-TERMS
import type { Deck } from "@deck/sdk";
type Storage = Deck["storage"];
interface WriteOptions {
  recover?: boolean;
  validatePrevious?: (value: unknown) => unknown;
}
interface Manifest {
  format: "deck-chunks-v1";
  generation: string;
  count: number;
  bytes: number;
  sha256: string;
}
export const MAX_STATE_BYTES = 2 * 1024 * 1024;
const MAX_CHUNK_BYTES = 240 * 1024 - 1;
const MAX_STORE_BYTES = 5 * 1024 * 1024;
const encoder = new TextEncoder();
const queues = new WeakMap<Storage, Map<string, Promise<void>>>();
function size(value: unknown): number {
  return encoder.encode(JSON.stringify(value)).length;
}
function corrupt(): never {
  throw new Error("저장 기록이 손상되었어요. 원본 기록 파일을 확인해 주세요.");
}
function manifest(value: unknown): Manifest | null {
  if (!value || typeof value !== "object" || !("format" in value)) return null;
  const m = value as Partial<Manifest>;
  if (
    m.format !== "deck-chunks-v1" ||
    typeof m.generation !== "string" ||
    !/^[a-f0-9-]{36}$/.test(m.generation) ||
    !Number.isInteger(m.count) ||
    (m.count ?? 0) < 1 ||
    (m.count ?? 21) > 20 ||
    !Number.isInteger(m.bytes) ||
    (m.bytes ?? 0) < 1 ||
    (m.bytes ?? MAX_STATE_BYTES + 1) > MAX_STATE_BYTES ||
    typeof m.sha256 !== "string" ||
    !/^[a-f0-9]{64}$/.test(m.sha256)
  )
    corrupt();
  return m as Manifest;
}
async function hash(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", encoder.encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
function chunkKey(key: string, generation: string, index: number): string {
  return `${key}.chunk.${generation}.${index}`;
}
async function readManifest(storage: Storage, key: string, m: Manifest): Promise<unknown> {
  let text = "";
  for (let i = 0; i < m.count; i++) {
    const chunk = await storage.get<{ generation: string; text: string }>(chunkKey(key, m.generation, i));
    if (!chunk || chunk.generation !== m.generation || typeof chunk.text !== "string" || size(chunk) > MAX_CHUNK_BYTES)
      corrupt();
    text += chunk.text;
    if (encoder.encode(text).length > MAX_STATE_BYTES) corrupt();
  }
  if (encoder.encode(text).length !== m.bytes || (await hash(text)) !== m.sha256) corrupt();
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return corrupt();
  }
}
export async function readChunked(storage: Storage, key: string): Promise<unknown | null> {
  return queued(storage, key, async () => {
    const value = await storage.get(key),
      m = manifest(value);
    return m ? readManifest(storage, key, m) : value;
  });
}
async function clean(storage: Storage, key: string, protectedGeneration: string | null): Promise<void> {
  const prefix = `${key}.chunk.`;
  for (const name of await storage.keys()) {
    if (name.startsWith(prefix) && (!protectedGeneration || !name.startsWith(`${prefix}${protectedGeneration}.`)))
      await storage.delete(name);
  }
}
function split(text: string, generation: string): string[] {
  const chunks: string[] = [];
  let offset = 0;
  while (offset < text.length) {
    let low = 1,
      high = Math.min(text.length - offset, MAX_CHUNK_BYTES);
    while (low < high) {
      const middle = Math.ceil((low + high) / 2);
      if (size({ generation, text: text.slice(offset, offset + middle) }) <= MAX_CHUNK_BYTES) low = middle;
      else high = middle - 1;
    }
    let end = offset + low;
    const last = text.charCodeAt(end - 1);
    if (end < text.length && last >= 0xd800 && last <= 0xdbff) end--;
    if (end <= offset) throw new Error("저장 기록을 나누지 못했어요.");
    chunks.push(text.slice(offset, end));
    offset = end;
  }
  return chunks;
}
async function write(storage: Storage, key: string, value: unknown, options: WriteOptions): Promise<void> {
  const text = JSON.stringify(value),
    bytes = encoder.encode(text).length;
  if (bytes > MAX_STATE_BYTES)
    throw new Error("기록은 2MB까지 저장할 수 있어요. 파일로 보관하고 새 작업을 시작해 주세요.");
  const previous = await storage.get(key);
  if (!options.recover) {
    const old = manifest(previous);
    const decoded = old ? await readManifest(storage, key, old) : previous;
    if (decoded !== null) options.validatePrevious?.(decoded);
    await clean(storage, key, old?.generation ?? null);
  }
  const generation = crypto.randomUUID(),
    chunks = split(text, generation);
  const next: Manifest = {
    format: "deck-chunks-v1",
    generation,
    count: chunks.length,
    bytes,
    sha256: await hash(text),
  };
  const store: Record<string, unknown> = {};
  for (const name of await storage.keys()) store[name] = await storage.get(name);
  for (let i = 0; i < chunks.length; i++) store[chunkKey(key, generation, i)] = { generation, text: chunks[i] };
  // Check both the staging state and final pointer against the actual whole-store serialization limit.
  if (size(store) > MAX_STORE_BYTES || size({ ...store, [key]: next }) > MAX_STORE_BYTES)
    throw new Error("저장 공간이 부족해요. 파일로 보관하고 기록을 정리해 주세요.");
  let pointerAttempted = false;
  try {
    for (let i = 0; i < chunks.length; i++)
      await storage.set(chunkKey(key, generation, i), { generation, text: chunks[i] });
    await readManifest(storage, key, next);
    pointerAttempted = true;
    await storage.set(key, next);
  } catch (error) {
    if (pointerAttempted) {
      // A failed response can follow a committed write. Never delete possibly referenced chunks.
      let current: unknown;
      try {
        current = await storage.get(key);
      } catch {
        throw error;
      }
      if (manifest(current)?.generation === generation) return;
    }
    for (let i = 0; i < chunks.length; i++) await storage.delete(chunkKey(key, generation, i)).catch(() => undefined);
    throw error;
  }
  await clean(storage, key, generation).catch(() => undefined);
}
export function writeChunked(storage: Storage, key: string, value: unknown, options: WriteOptions = {}): Promise<void> {
  return queued(storage, key, () => write(storage, key, value, options));
}
function queued<T>(storage: Storage, key: string, action: () => Promise<T>): Promise<T> {
  let queue = queues.get(storage);
  if (!queue) {
    queue = new Map();
    queues.set(storage, queue);
  }
  const prior = queue.get(key) ?? Promise.resolve();
  const running = prior.then(action);
  const gate = running.then(
    () => undefined,
    () => undefined,
  );
  queue.set(key, gate);
  return running.finally(() => {
    if (queue.get(key) === gate) queue.delete(key);
  });
}
