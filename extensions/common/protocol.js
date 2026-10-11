"use strict";

const CHUNK_BYTES = 192 * 1024;
const MAX_PAYLOAD = 64 * 1024 * 1024;

function wireFrames(message) {
  const raw = new TextEncoder().encode(JSON.stringify(message));
  if (raw.length > MAX_PAYLOAD) throw new Error("payload-limit");
  const key = crypto.randomUUID();
  const total = Math.ceil(raw.length / CHUNK_BYTES);
  const frames = [];
  for (let index = 0; index < total; index++) {
    const part = raw.subarray(index * CHUNK_BYTES, (index + 1) * CHUNK_BYTES);
    const binary = Array.from(part, byte => String.fromCharCode(byte)).join("");
    frames.push({type: "chunk", key, index, total, data: btoa(binary)});
  }
  return frames;
}

class WireAssembler {
  constructor() { this.pending = new Map(); }
  feed(frame) {
    if (frame.type !== "chunk" || typeof frame.key !== "string" ||
        !Number.isInteger(frame.total) || !Number.isInteger(frame.index) ||
        frame.total < 1 || frame.total > Math.floor(MAX_PAYLOAD / CHUNK_BYTES) + 1 ||
        frame.index < 0 || frame.index >= frame.total) throw new Error("invalid-frame");
    if (!this.pending.has(frame.key) && this.pending.size >= 4) throw new Error("frame-limit");
    const bucket = this.pending.get(frame.key) || {total: frame.total, parts: new Map()};
    this.pending.set(frame.key, bucket);
    if (bucket.total !== frame.total || bucket.parts.has(frame.index)) throw new Error("duplicate-frame");
    const raw = Uint8Array.from(atob(frame.data), char => char.charCodeAt(0));
    if (raw.length > CHUNK_BYTES) throw new Error("chunk-limit");
    bucket.parts.set(frame.index, raw);
    if (bucket.parts.size !== frame.total) return null;
    let length = 0;
    for (const part of bucket.parts.values()) length += part.length;
    if (length > MAX_PAYLOAD) throw new Error("payload-limit");
    const all = new Uint8Array(length);
    let offset = 0;
    for (let i = 0; i < frame.total; i++) {
      all.set(bucket.parts.get(i), offset);
      offset += bucket.parts.get(i).length;
    }
    this.pending.delete(frame.key);
    return JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(all));
  }
}

function cookieKey(cookie) {
  const partition = cookie.partitionKey;
  return JSON.stringify([cookie.domain, cookie.path || "/", cookie.name,
    partition ? partition.topLevelSite : null,
    partition ? Boolean(partition.hasCrossSiteAncestor) : null]);
}

function cookieDetails(cookie, expirationOverride) {
  const host = cookie.domain.replace(/^\./, "");
  const path = cookie.path || "/";
  const urlPath = encodeURI(path).replace(/[?#]/g, char => encodeURIComponent(char));
  const details = {url: `${cookie.secure ? "https" : "http"}://${host}${urlPath}`,
    name: cookie.name, value: cookie.value || "", path,
    secure: Boolean(cookie.secure), httpOnly: Boolean(cookie.httpOnly),
    sameSite: cookie.sameSite || "unspecified"};
  if (!cookie.hostOnly) details.domain = cookie.domain;
  if (cookie.partitionKey) details.partitionKey = {...cookie.partitionKey};
  if (expirationOverride !== undefined) details.expirationDate = expirationOverride;
  else if (!cookie.session && cookie.expirationDate !== undefined) details.expirationDate = cookie.expirationDate;
  // Never copy storeId: source and destination stores are different namespaces.
  return details;
}

if (typeof module !== "undefined") module.exports = {wireFrames, WireAssembler, cookieKey, cookieDetails};
