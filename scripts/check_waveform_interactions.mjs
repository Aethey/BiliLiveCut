#!/usr/bin/env node
/** Check the real waveform loader without waiting for piped stdin to reach EOF. */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const template = await readFile(process.argv[2] ?? new URL("../app/web/templates/review.html", import.meta.url), "utf8");
const start = template.indexOf("async function loadWaveform()");
const end = template.indexOf("function drawWaveform(", start);
assert.ok(start >= 0 && end > start, "waveform loader boundaries must exist");
console.log("CHECK: waveform template loaded");
// Execute only the tracked loader and deterministic API/DOM boundaries, not the page startup.
const checks = `
import assert from "node:assert/strict";
const errorNode = {textContent: ""};
const $ = () => errorNode;
let waveformPeaks = [], waveformDuration = 0;
let payload = {error: "<bad media>", peaks: []};
let failure = false;
const api = async () => { if (failure) throw new Error("HTTP 500"); return payload; };
const drawWaveform = () => {};
const CAND_ID = 1;
${template.slice(start, end)}
await loadWaveform();
assert.match(errorNode.textContent, /<bad media>/);
console.log("CHECK: waveform media failure displayed");
waveformPeaks = [0.9];
failure = true;
await loadWaveform();
assert.match(errorNode.textContent, /HTTP 500/);
assert.deepEqual(waveformPeaks, []);
console.log("CHECK: waveform request failure cleared stale peaks");
failure = false;
payload = {peaks: [0.1], duration_s: 2};
await loadWaveform();
assert.equal(errorNode.textContent, "");
assert.deepEqual(waveformPeaks, [0.1]);
assert.equal(waveformDuration, 2);
console.log("PASS: waveform failure and recovery");
`;
await import(`data:text/javascript;base64,${Buffer.from(checks).toString("base64")}`);
