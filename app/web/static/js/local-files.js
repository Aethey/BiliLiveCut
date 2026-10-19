// 设置中心：在运行 BiliLiveCut 的电脑上打开已知的本地目录。
import { $, api, esc } from "./common.js";

const trigger = $("#local-files-trigger");
const dialog = $("#local-files-dialog");
const closeButton = $("#local-files-close");
const list = $("#local-files-list");
const status = $("#local-files-status");

async function loadFolders() {
  list.innerHTML = '<p class="hint">正在读取文件夹…</p>';
  status.textContent = "";
  try {
    const folders = await api("GET", "/api/settings/local-folders");
    list.innerHTML = folders.map(folder => `
      <button type="button" class="local-files-folder" data-folder-id="${esc(folder.id)}">
        <span class="local-files-folder-name">📁 ${esc(folder.name)}</span>
        <span class="local-files-folder-desc">${esc(folder.description)}</span>
        <span class="local-files-folder-path">${esc(folder.path)}</span>
        <span class="local-files-folder-action">${folder.exists ? "打开文件夹 ↗" : "创建并打开 ↗"}</span>
      </button>`).join("");
  } catch (error) {
    list.innerHTML = '<p class="empty">读取文件夹失败</p>';
    status.textContent = error.message;
  }
}

trigger.addEventListener("click", () => {
  dialog.showModal();
  void loadFolders();
});

closeButton.addEventListener("click", () => dialog.close());
list.addEventListener("click", async event => {
  const button = event.target.closest?.("[data-folder-id]");
  if (!button || button.disabled) return;
  button.disabled = true;
  status.textContent = "正在请求打开文件夹…";
  try {
    await api("POST", `/api/settings/local-folders/${encodeURIComponent(button.dataset.folderId)}/open`);
    await loadFolders();
    status.textContent = "已请求在运行 BiliLiveCut 的电脑上打开文件夹。";
  } catch (error) {
    status.textContent = `打开失败：${error.message}`;
  } finally {
    button.disabled = false;
  }
});
