// 录播分析：工作台标签与独立页面共用分步上传、持久作业和分析结果导航。
import { $, api, esc } from "./common.js";

let records = [];
let draft = null;
let pendingDeclaration = null;
let batch = null;
let automaticTitle = "";
let operationRevision = 0;
let busy = false;
let cancelled = false;
let activeUpload = null;
let listLoading = false;
let listSignature = null;
let actionPending = false;
let actionError = "";
let pageActive = true;
let pollTimer = null;
let videoLimit = 50 * 1024 ** 3;
let commentLimit = 32 * 1024 ** 2;

export function hasRecordingImportDraft() {
  return busy || Boolean(batch) || Boolean($("#import-video").files?.length || $("#import-comments").files?.length)
    || (!draft && !pendingDeclaration && Boolean($("#import-title").value.trim()));
}

export function uploadFile(path, file, onProgress, onRequest) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("PUT", path);
    request.setRequestHeader("Content-Type", "application/octet-stream");
    request.upload.onprogress = event => {
      if (event.lengthComputable) onProgress(Math.round(event.loaded / event.total * 100));
    };
    request.onload = () => {
      let data;
      try { data = JSON.parse(request.responseText); } catch (_error) { reject(new Error("服务器返回无效响应")); return; }
      if (request.status >= 200 && request.status < 300) resolve(data);
      else reject(new Error(typeof data.detail === "string" ? data.detail : `上传失败（${request.status}）`));
    };
    request.onerror = () => reject(new Error("上传连接中断，可从导入记录继续"));
    request.onabort = () => reject(new Error("上传已停止，可重新选择未完成文件继续"));
    onRequest(request);
    request.send(file);
  });
}

function fileStem(name) { return name.replace(/\.[^.]+$/, ""); }
function matchKey(name) { return name.normalize("NFC").toLowerCase(); }

function validateFile(file, expectedName, limit, label) {
  if (!file) throw new Error(`请选择${label}`);
  if (expectedName && file.name !== expectedName) throw new Error(`请重新选择 ${expectedName}`);
  if (!Number.isSafeInteger(file.size) || file.size <= 0 || file.size > limit) throw new Error(`${label}为空或超过大小限制：${file.name}`);
  if (!file.name || file.name.length > 255 || /[/\\\u0000-\u001f]/.test(file.name)) throw new Error(`文件名不支持：${file.name}`);
}

/** Pair files before any upload; never guess when a name identifies several inputs. */
export function planRecordingImports(videos, comments) {
  if (!videos.length) throw new Error("请选择视频文件");
  const byStem = new Map();
  const byName = new Map();
  const rows = videos.map(video => {
    validateFile(video, null, videoLimit, "视频文件");
    if (!/\.(mp4|mkv|flv|ts|mov|webm|m4v)$/i.test(video.name)) throw new Error(`视频格式不支持：${video.name}`);
    if (!fileStem(video.name).trim()) throw new Error(`视频主文件名不能为空：${video.name}`);
    const key = matchKey(fileStem(video.name));
    if (byStem.has(key)) throw new Error(`视频主文件名重复：${video.name}。请更名或分批导入。`);
    const row = { video, comments: null, status: "等待上传", submitted: false, record: null, declaration: null };
    byStem.set(key, row); byName.set(matchKey(video.name), row);
    return row;
  });
  const seenComments = new Set();
  for (const file of comments) {
    validateFile(file, null, commentLimit, "弹幕文件");
    if (!/\.(xml|json|srt|ass)$/i.test(file.name)) throw new Error(`弹幕格式不支持：${file.name}`);
    const key = matchKey(fileStem(file.name));
    if (seenComments.has(key)) throw new Error(`同名弹幕有多个文件：${file.name}。每段视频只能选择一份弹幕。`);
    seenComments.add(key);
    // A single explicitly selected pair retains the existing manual-pairing workflow.
    const candidates = rows.length === 1 && comments.length === 1 ? [rows[0]]
      : [...new Set([byStem.get(key), byName.get(key)].filter(Boolean))];
    if (!candidates.length) throw new Error(`弹幕没有对应视频：${file.name}`);
    if (candidates.length !== 1 || candidates[0].comments) throw new Error(`弹幕配对不唯一：${file.name}。请保留唯一同名文件。`);
    candidates[0].comments = file;
  }
  const order = new Intl.Collator("zh-CN", { numeric: true, sensitivity: "base" });
  return rows.sort((a, b) => order.compare(a.video.name, b.video.name));
}

function renderPlan() {
  const preview = $("#import-preview");
  const error = $("#import-pairing-error");
  error.textContent = "";
  let rows = batch?.items;
  if (!rows) {
    const videos = Array.from($("#import-video").files || []);
    const comments = Array.from($("#import-comments").files || []);
    if (!videos.length) { preview.hidden = true; return; }
    try { rows = planRecordingImports(videos, comments); }
    catch (failure) { preview.hidden = true; error.textContent = failure.message; return; }
  }
  preview.hidden = false;
  preview.innerHTML = `<p>共 ${rows.length} 段视频 · 已提交 ${rows.filter(row => row.submitted).length} 段</p><ol>${rows.map(row =>
    `<li><strong>${esc(row.video?.name || row.record?.video_name)}</strong> → ${esc(row.comments?.name || row.record?.comments_name || "无弹幕")}<span class="hint"> · ${esc(row.status)}</span></li>`
  ).join("")}</ol>`;
}

function renderDraft() {
  const declaration = draft || pendingDeclaration;
  const multiple = batch?.items.length > 1;
  $("#import-fields").disabled = busy;
  $("#import-title").disabled = Boolean(batch || declaration);
  $("#import-offset").disabled = Boolean(batch || declaration);
  $("#import-video").disabled = Boolean(multiple || draft?.video_uploaded);
  $("#import-video").required = !multiple && !draft?.video_uploaded;
  $("#import-comments").disabled = Boolean(multiple || (declaration && (!declaration.comments_name || draft?.comments_uploaded)));
  $("#import-comments").required = Boolean(!multiple && declaration?.comments_name && !draft?.comments_uploaded);
  $("#import-submit").disabled = busy;
  $("#import-submit").textContent = batch || declaration ? "继续上传并分析" : "导入并开始分析";
  $("#import-new").hidden = !(batch || declaration) || busy;
  $("#import-resume").hidden = !declaration;
  $("#import-resume").textContent = declaration
    ? `继续「${declaration.title}」：视频 ${draft?.video_uploaded ? "已上传" : declaration.video_name}；弹幕 ${draft?.comments_uploaded ? "已上传" : declaration.comments_name || "未选择"}。已上传文件无需重选。更换素材请新建导入。`
    : "";
  renderPlan();
}

function resetForm() {
  draft = null;
  pendingDeclaration = null;
  batch = null;
  automaticTitle = "";
  $("#import-form").reset();
  $("#import-progress").hidden = true;
  $("#import-error").textContent = "";
  renderDraft();
}

function prepareBatch() {
  const videos = Array.from($("#import-video").files || []);
  const comments = Array.from($("#import-comments").files || []);
  if (draft || pendingDeclaration) {
    if (videos.length > 1 || comments.length > 1) throw new Error("继续已有导入时，请只选择该记录对应的未完成文件");
    return { index: 0, items: [{ video: videos[0], comments: comments[0], record: draft, declaration: pendingDeclaration,
      submitted: false, status: "等待继续" }] };
  }
  const offset = Number($("#import-offset").value || 0);
  if (!Number.isFinite(offset) || Math.abs(offset) > 604800) throw new Error("弹幕偏移必须在 -604800 到 604800 秒之间");
  const rows = planRecordingImports(videos, comments);
  const title = $("#import-title").value.trim();
  for (const row of rows) {
    const name = fileStem(row.video.name);
    row.declaration = {
      title: (rows.length === 1 ? title || name : title ? `${title} - ${name}` : name).slice(0, 200),
      video_name: row.video.name, comments_name: row.comments?.name || null, offset_s: offset,
      request_id: Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join(""),
    };
  }
  return { items: rows, index: 0 };
}

async function submitImport(event) {
  event.preventDefault();
  if (busy || actionPending) return;
  $("#import-error").textContent = "";
  try {
    batch ||= prepareBatch();
    const total = batch.items.length;
    // A resumed server record may need fresh browser File objects; batch snapshots stay immutable.
    if (total === 1) {
      const entry = batch.items[0];
      const videos = Array.from($("#import-video").files || []);
      const comments = Array.from($("#import-comments").files || []);
      if (videos.length > 1 || comments.length > 1) throw new Error("继续已有导入时，请只选择对应文件");
      entry.video = videos[0] || entry.video;
      entry.comments = comments[0] || entry.comments;
    }
    busy = true;
    operationRevision += 1;
    cancelled = false;
    for (; batch.index < total; batch.index += 1) {
      const entry = batch.items[batch.index];
      draft = entry.record;
      pendingDeclaration = entry.declaration;
      const declaration = draft || pendingDeclaration;
      const prefix = `${batch.index + 1}/${total} · ${declaration.video_name}`;
      if (cancelled) throw new Error("上传已停止");
      if (!draft?.video_uploaded) validateFile(entry.video, declaration.video_name, videoLimit, "视频文件");
      if (declaration.comments_name && !draft?.comments_uploaded) validateFile(entry.comments, declaration.comments_name, commentLimit, "弹幕文件");
      entry.status = "正在准备";
      renderDraft();
      $("#import-cancel").hidden = false;
      $("#import-status").textContent = `${prefix}：正在准备上传…`;
      if (!draft) { draft = await api("POST", "/api/recording-imports", pendingDeclaration); entry.record = draft; }
      for (const [kind, file, label] of [["video", entry.video, "视频"], ["comments", entry.comments, "弹幕"]]) {
        if (draft[`${kind}_uploaded`] || (kind === "comments" && !draft.comments_name)) continue;
        if (cancelled) throw new Error("上传已停止");
        entry.status = `正在上传${label}`;
        renderPlan();
        $("#import-progress").hidden = false;
        $("#import-progress").value = 0;
        $("#import-status").textContent = `${prefix}：正在上传${label} 0%`;
        draft = await uploadFile(`/api/recording-imports/${draft.id}/files/${kind}`, file, percent => {
          $("#import-progress").value = percent;
          $("#import-status").textContent = `${prefix}：正在上传${label} ${percent}%`;
        }, request => { activeUpload = request; });
        entry.record = draft;
        activeUpload = null;
      }
      if (cancelled) throw new Error("上传已停止");
      entry.status = "正在提交预处理";
      renderPlan();
      $("#import-status").textContent = `${prefix}：上传完成，正在提交预处理…`;
      await api("POST", `/api/recording-imports/${draft.id}/start`);
      entry.submitted = true;
      entry.status = "已提交分析";
      // Release large File references after a successful submission, keeping only names for the preview.
      entry.video = null;
      entry.comments = null;
    }
    $("#import-status").textContent = `已提交 ${total} 段录播。预处理和分析会在后台继续，可在下方查看进度。`;
    resetForm();
  } catch (error) {
    const entry = batch?.items[batch.index];
    if (entry) { entry.status = cancelled ? "已停止，可继续" : "失败，可继续"; entry.record = draft; }
    $("#import-error").textContent = error.message;
    $("#import-status").textContent = batch
      ? "未完成队列会保留。点击继续可跳过已完成文件；关闭页面后可从下方记录恢复已创建的导入。"
      : "请修正文件配对或输入内容后重试。";
  } finally {
    busy = false;
    activeUpload = null;
    $("#import-cancel").hidden = true;
    renderDraft();
    await refreshImports();
  }
}

export function importState(record) {
  if (record.session_id) {
    const progress = record.analysis;
    if (progress?.failed) return "部分分析失败";
    if (progress?.retrying) return "等待自动重试";
    if (progress?.asr_unavailable) return "转写证据不完整";
    return progress?.total && progress.done === progress.total ? "分析完成" : "分析中";
  }
  return { queued: "等待预处理", running: "正在预处理", cancelling: "正在停止",
    cancelled: "预处理已取消", failed: "预处理失败", succeeded: "预处理完成" }[record.job?.status]
    || (record.video_uploaded ? "等待提交" : "等待上传");
}

function recordCard(record) {
  const status = importState(record);
  const job = record.job;
  const progress = record.analysis;
  const activeJob = ["queued", "running", "cancelling"].includes(job?.status);
  const action = (name, label) => `<button data-action="${name}" data-id="${esc(record.id)}">${label}</button>`;
  const controls = record.session_id
    ? `<a data-id="${esc(record.id)}" data-action="timeline" href="/?tab=candidates&amp;session_id=${record.session_id}">查看热点时间线</a>
       <a data-id="${esc(record.id)}" data-action="transcripts" href="/?tab=transcripts&amp;session_id=${record.session_id}">查看转写</a><a data-id="${esc(record.id)}" data-action="tasks" href="/?tab=tasks">全部任务与重试</a>`
    : activeJob ? (job.status === "cancelling" ? "" : action("cancel", "取消预处理"))
    : ["failed", "cancelled"].includes(job?.status) ? action("retry", "重试预处理") + action("discard", "清理导入")
    : action("resume", record.sealed ? "重新提交" : "继续导入") + action("discard", "清理导入");
  return `<article class="card import-record">
    <div class="head"><h3>${esc(record.title)}</h3><span class="badge ${status.includes("失败") ? "red" : status === "分析完成" ? "green" : "gray"}">${status}</span></div>
    <p class="hint">${esc(record.video_name)} · ${record.comments_name ? esc(record.comments_name) : "无弹幕文件"} · ${esc(new Date(record.created_at).toLocaleString("zh-CN"))}</p>
    ${progress ? `<p>已分析 ${progress.done} / ${progress.total} 段 · 转写记录 ${progress.transcripts} 段${progress.failed ? ` · ${progress.failed} 段需检查` : ""}${progress.retrying ? ` · ${progress.retrying} 段等待重试` : ""}</p>` : ""}
    ${progress?.asr_unavailable ? `<p class="warn">${progress.asr_unavailable} 段语音证据不可用或质量不足。请检查语音转写设置和任务详情，修复后重新识别。</p>` : ""}
    ${record.session_id ? `<p class="hint">会话 #${record.session_id} · ${record.segment_count} 个分段 · ${record.comment_count} 条文本事件</p>` : ""}
    ${activeJob ? `<progress max="100" value="${Number(job.progress) || 0}" aria-label="预处理进度"></progress><p class="hint">${esc(job.message)}</p>` : ""}
    ${job?.error ? `<p class="warn">${esc(job.error)}</p>` : ""}
    ${(record.warnings || []).map(warning => `<p class="warn">${esc(warning)}</p>`).join("")}
    <div class="row import-actions">${controls}</div></article>`;
}

export async function refreshImports() {
  if (busy || listLoading || actionPending) return;
  listLoading = true;
  const revision = operationRevision;
  try {
    const data = await api("GET", "/api/recording-imports");
    if (revision !== operationRevision) return;
    records = data.imports;
    videoLimit = data.max_video_bytes;
    commentLimit = data.max_comment_bytes;
    const engines = { funasr_nano: "Fun-ASR-Nano", funasr: "Fun-ASR-Nano", nano: "Fun-ASR-Nano", paraformer: "Paraformer", whisper: "Whisper", openai_compatible: "远程转写（OpenAI 兼容）" };
    $("#import-engine").textContent = `本地主引擎：${engines[data.engine] || data.engine}`;
    $("#imports-list-error").textContent = actionError;
    if (!busy && draft) {
      const latest = records.find(record => record.id === draft.id);
      if (batch) {
        if (latest) { draft = latest; batch.items[batch.index].record = latest; }
        renderDraft();
      } else if (latest?.sealed || !latest) resetForm();
      else { draft = latest; renderDraft(); }
    }
    const signature = JSON.stringify(records);
    if (signature !== listSignature) {
      const focused = document.activeElement?.dataset;
      $("#imports-list").innerHTML = records.length ? records.map(recordCard).join("")
        : '<div class="card empty">还没有导入记录。选择一段视频，开始第一次录播分析。</div>';
      listSignature = signature;
      if (focused?.id && focused.action) {
        $("#imports-list").querySelector(`[data-id="${focused.id}"][data-action="${focused.action}"]`)?.focus();
      }
    }
  } catch (error) {
    $("#imports-list-error").textContent = `读取失败：${error.message}`;
    if (listSignature === null) {
      $("#imports-list").innerHTML = '<div class="card empty">导入记录读取失败，请点击“刷新状态”重试。</div>';
    }
  }
  finally { listLoading = false; }
}

function selectionChanged() {
  if (!batch && !draft && !pendingDeclaration) {
    $("#import-error").textContent = "";
    $("#import-status").textContent = "";
  }
  renderPlan();
}

$("#import-form").addEventListener("submit", submitImport);
$("#import-video").addEventListener("change", () => {
  if (!draft && !pendingDeclaration && !batch) {
    const files = Array.from($("#import-video").files || []);
    const title = $("#import-title");
    if (!title.value || title.value === automaticTitle) {
      automaticTitle = files.length === 1 ? fileStem(files[0].name).slice(0, 200) : "";
      title.value = automaticTitle;
    }
  }
  selectionChanged();
});
$("#import-comments").addEventListener("change", selectionChanged);
$("#import-cancel").addEventListener("click", () => { cancelled = true; activeUpload?.abort(); });
$("#import-new").addEventListener("click", () => {
  if (busy || actionPending) return;
  if (batch?.items.length > 1 && !window.confirm("放弃本页尚未上传的队列并重新选择？已创建的导入仍保留在下方。")) return;
  operationRevision += 1;
  resetForm();
});
$("#imports-refresh").addEventListener("click", refreshImports);
$("#imports-list").addEventListener("click", async event => {
  const button = event.target.closest("button[data-action]");
  if (!button || busy || actionPending) return;
  const record = records.find(item => item.id === button.dataset.id);
  if (!record) return;
  actionPending = true;
  operationRevision += 1;
  actionError = "";
  button.disabled = true;
  try {
    if (button.dataset.action === "resume") {
      if (record.sealed) await api("POST", `/api/recording-imports/${record.id}/start`);
      else {
        if (batch?.items.length > 1 && !window.confirm("切换到这条导入会放弃本页待上传队列，是否继续？")) return;
        resetForm();
        draft = record;
        $("#import-title").value = record.title;
        $("#import-offset").value = record.offset_s;
        renderDraft();
        $("#import-form").scrollIntoView({ behavior: "smooth", block: "start" });
        $(!record.video_uploaded ? "#import-video" : record.comments_name && !record.comments_uploaded ? "#import-comments" : "#import-submit").focus();
      }
    } else if (button.dataset.action === "discard") {
      const queueNotice = batch?.items.length > 1 && draft?.id === record.id ? "这也会放弃本页剩余待上传队列。" : "";
      if (!window.confirm(`清理「${record.title}」的未完成导入？${queueNotice}原始文件不受影响。`)) return;
      await api("DELETE", `/api/recording-imports/${record.id}`);
      if (draft?.id === record.id) resetForm();
    } else {
      await api("POST", `/api/jobs/${record.job.id}/${button.dataset.action}`);
    }
  } catch (error) { actionError = error.message; $("#imports-list-error").textContent = actionError; }
  finally { actionPending = false; button.disabled = false; await refreshImports(); }
});
window.addEventListener("beforeunload", event => {
  if (!hasRecordingImportDraft()) return;
  event.preventDefault(); event.returnValue = "";
});
async function poll() {
  if (!document.hidden) await refreshImports();
  if (pageActive) pollTimer = window.setTimeout(poll, 3000);
}
renderDraft();
if (document.querySelector("body.imports-page")) {
  window.addEventListener("pagehide", () => { pageActive = false; window.clearTimeout(pollTimer); });
  window.addEventListener("pageshow", event => { if (event.persisted) { pageActive = true; poll(); } });
  poll();
}
