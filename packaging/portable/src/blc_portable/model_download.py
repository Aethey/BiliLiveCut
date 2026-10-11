"""模型仓库的有限重试与安全诊断，不依赖业务 Payload 或修改 SDK 全局配置。"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import random
import time
import traceback
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from functools import partial
from pathlib import Path

from blc_portable.diagnostics import redact_diagnostic, safe_host
from blc_portable.launcher.progress import ModelScopeProgress, emit_progress, hf_progress_class

ATTEMPTS_PER_SOURCE = 2
HF_MIRRORS = ["https://hf-mirror.com", "https://huggingface.co"]
MODELSCOPE_MIRRORS = ["https://www.modelscope.cn"]


def download_hf_snapshot(repo_id: str, target_dir: Path, revision: str | None, endpoint: str | None) -> None:
    """显式绑定 SDK 支持的端点，禁止将本机缓存令牌发送给模型镜像。"""
    from huggingface_hub import snapshot_download

    # SDK 1.26 的请求/退避日志也会输出签名 URL；在来源 logger 处脱敏，
    # 使其自身 handler 和向上传播的 handler 都只接收安全诊断。
    with _safe_sdk_logs():
        emit_progress("prepare", repo_id + "：获取文件清单")
        progress = {"tqdm_class": hf_progress_class()} if os.environ.get("BLC_MODEL_PROGRESS") == "1" else {}
        snapshot_download(
            repo_id=repo_id, revision=revision, local_dir=str(target_dir), endpoint=endpoint, token=False, **progress
        )
        emit_progress("verify", repo_id)


def download_ms_snapshot(model_id: str, target_dir: Path, revision: str | None, endpoint: str) -> None:
    """核对主/子模型的完整官方文件清单，拒绝 SDK 吞掉文件失败后的半下载目录。"""
    from modelscope_hub.api import HubApi
    from modelscope_hub.compat.snapshot_download import snapshot_download

    with _safe_sdk_logs():
        emit_progress("prepare", model_id + "：获取文件清单")
        files = [
            item
            for item in HubApi(endpoint=endpoint).list_repo_files(model_id, "model", revision=revision, recursive=True)
            if not item.is_dir
        ]
        if not files:
            raise RuntimeError("phase=verify：模型仓库返回空文件清单")
        root = target_dir.resolve()
        for item in files:
            path = (root / item.path).resolve()
            if path == root or not path.is_relative_to(root):
                raise RuntimeError("phase=verify：模型文件路径越界")
            # SDK 将缺失 Size 归一化为 0；合法空文件必须有 SHA256 证明。
            if item.size <= 0 and not item.sha256:
                raise RuntimeError(f"phase=verify：模型文件缺少大小和 SHA256：{item.path}")
        progress = {"progress_callbacks": [ModelScopeProgress]} if os.environ.get("BLC_MODEL_PROGRESS") == "1" else {}
        snapshot_download(
            model_id=model_id, revision=revision, local_dir=str(target_dir), endpoint=endpoint, **progress
        )
        emit_progress("verify", model_id)
        for item in files:
            path = (root / item.path).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise RuntimeError(f"phase=verify：模型下载缺少文件：{item.path}")
            if item.size is not None and path.stat().st_size != item.size:
                raise RuntimeError(f"phase=verify：模型文件大小不符：{item.path}")
            if item.sha256:
                with path.open("rb") as handle:
                    actual = hashlib.file_digest(handle, "sha256").hexdigest()
                if actual.lower() != item.sha256.lower():
                    raise RuntimeError(f"phase=verify：模型文件 SHA256 不符：{item.path}")


def download_ms_model(engine: str, model: str, target_dir: Path, revision: str | None) -> None:
    """Engine Pack 下载与启动器使用相同的 ModelScope 有限重试和完整性边界。"""
    download_with_retry(
        engine=engine,
        model=model,
        revision=revision,
        endpoints=MODELSCOPE_MIRRORS,
        operation=partial(download_ms_snapshot, model, target_dir, revision),
    )


@contextmanager
def _safe_sdk_logs() -> Iterator[None]:
    """为锁定 SDK 的请求/下载 logger 安装临时脱敏边界。"""
    sources = [
        logging.getLogger(name)
        for name in ("huggingface_hub.utils._http", "httpx", "modelscope_hub.download", "urllib3.connectionpool")
    ]
    diagnostic_filter = _DiagnosticFilter()
    for source in sources:
        source.addFilter(diagnostic_filter)
    try:
        yield
    finally:
        for source in sources:
            source.removeFilter(diagnostic_filter)


class _DiagnosticFilter(logging.Filter):
    """在 SDK 请求日志离开来源 logger 前脱敏，保留级别与根异常类型。"""

    def filter(self, record: logging.LogRecord) -> bool:
        """替换参数展开后的内容和异常堆栈，防止后续 formatter 重建原文。"""
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + "".join(traceback.format_exception(*record.exc_info))
            record.exc_info = None
            record.exc_text = None
        if record.stack_info:
            record.stack_info = redact_diagnostic(record.stack_info)
        record.msg = redact_diagnostic(message)
        record.args = ()
        return True


def _exception_chain(error: BaseException) -> Iterator[BaseException]:
    """遍历 SDK 包装异常且防止异常链循环。"""
    seen: set[int] = set()
    while id(error) not in seen:
        seen.add(id(error))
        yield error
        nested = error.__cause__ or error.__context__
        if nested is None:
            break
        error = nested


def _failure_kind(error: Exception) -> tuple[str, bool, str]:
    """按本地错误、HTTP 状态和连接错误分流，未知错误不盲目切源。"""
    import httpx
    from huggingface_hub.errors import FileMetadataError
    from requests.exceptions import ConnectionError as RequestsConnectionError
    from requests.exceptions import Timeout as RequestsTimeout

    chain = list(_exception_chain(error))
    for item in chain:
        if isinstance(item, OSError) and item.errno in {
            errno.ENOSPC,
            errno.EDQUOT,
            errno.EACCES,
            errno.EPERM,
            errno.EROFS,
        }:
            return "local_io", False, "检查磁盘剩余空间和模型目录写入权限后再启动"
    if any(isinstance(item, (httpx.UnsupportedProtocol, httpx.LocalProtocolError)) for item in chain):
        return "configuration", False, "检查下载端点和代理协议配置后重新启动"
    for item in chain:
        response = getattr(item, "response", None)
        status = getattr(response, "status_code", None)
        if status in (408, 429) or isinstance(status, int) and 500 <= status <= 599:
            return f"http_{status}", True, "检查网络、代理和上游服务，或提供当前版本的 Engine Pack"
        if isinstance(status, int) and 400 <= status <= 499:
            return f"http_{status}", False, "核对仓库访问权限及锁定的模型/revision；不会把 HTTP 错误当作连接超时"
    if any(isinstance(item, FileMetadataError) for item in chain):
        return (
            "metadata",
            True,
            "端点未提供 SDK 要求的固定 revision 文件元数据；切换下一已配置端点，或使用内容兼容的 Engine Pack",
        )
    if any(
        isinstance(
            item, (httpx.TransportError, RequestsConnectionError, RequestsTimeout, TimeoutError, ConnectionError)
        )
        for item in chain
    ):
        return "network", True, "检查网络和代理，或提供当前版本的 Engine Pack"
    return "non_retryable", False, "根据根异常检查依赖、模型/revision 和本地文件；修复后重新启动"


def download_with_retry(
    *,
    engine: str,
    model: str,
    revision: str | None,
    endpoints: Sequence[str],
    operation: Callable[[str], None],
) -> None:
    """网络错误每源最多两次；元数据协议异常直接切源，内容身份始终锁定。"""
    if not endpoints:
        raise ValueError("模型下载未配置端点")
    for source_index, endpoint in enumerate(endpoints):
        for attempt in range(1, ATTEMPTS_PER_SOURCE + 1):
            try:
                operation(endpoint)
                return
            except Exception as exc:  # SDK 边界：分类后有限重试或保留异常链重新抛出。
                kind, recoverable, action = _failure_kind(exc)
                chain = list(_exception_chain(exc))
                root = chain[-1]
                failed_host = "unknown"
                for item in reversed(chain):
                    try:
                        request = getattr(item, "request", None)
                    except RuntimeError:  # HTTPX 未绑定 request 的异常实例。
                        request = None
                    if request is not None:
                        failed_host = safe_host(str(request.url))
                        break
                retry_same_source = recoverable and kind != "metadata" and attempt < ATTEMPTS_PER_SOURCE
                retry = recoverable and (retry_same_source or source_index + 1 < len(endpoints))
                root_detail = redact_diagnostic(str(root), limit=1024)
                summary = redact_diagnostic(
                    f"模型准备失败 engine={engine} model={model} revision={revision} phase=download "
                    f"endpoint={safe_host(endpoint)} failed_host={failed_host} kind={kind} "
                    f"source={source_index + 1}/{len(endpoints)} attempt={attempt}/{ATTEMPTS_PER_SOURCE} "
                    f"retry={retry} root={type(root).__name__}: {root_detail}; action={action}"
                )
                if not retry:
                    raise RuntimeError(summary) from exc
                print(summary, flush=True)
                if not retry_same_source:
                    break
                if retry_same_source:
                    time.sleep(random.uniform(0.8, 1.2))
