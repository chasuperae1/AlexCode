"""基于 Git 影子仓库的文件快照，用于 /undo、/redo 与 /checkpoints。

参考 opencode 的做法：快照存放在工作区之外的独立 Git 对象库里，
通过 ``GIT_DIR`` + ``GIT_WORK_TREE`` 操作，不碰用户自己的 ``.git``。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from alex.config import CONFIG, ROOT

DEFAULT_EXCLUDES = [
    ".git/**",
    "node_modules/**",
    ".venv/**",
    "venv/**",
    "__pycache__/**",
    "*.pyc",
    "dist/**",
    "build/**",
]

# 兜底排除：避免把会话、记忆和快照自身写进快照
MANDATORY_EXCLUDES = [
    ".env",
    "**/.env",
    "data/snapshots/**",
    "data/sessions/**",
    "data/Memory/**",
]


class SnapshotError(RuntimeError):
    """快照操作失败。"""


@dataclass
class Checkpoint:
    """一次对话交换对应的文件快照。"""

    before: str
    after: str
    context_start: int
    history_start: int
    prompt: str
    created_at: str
    files: list[str] = field(default_factory=list)
    exchange_id: int = 0
    additions: int = 0
    deletions: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_input_tokens: int = 0
    duration: float = 0.0
    compacted: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Checkpoint":
        return cls(
            before=str(data["before"]),
            after=str(data["after"]),
            context_start=int(data.get("context_start", 0)),
            history_start=int(data.get("history_start", 0)),
            prompt=str(data.get("prompt", "")),
            created_at=str(data.get("created_at", "")),
            files=[str(item) for item in data.get("files", [])],
            exchange_id=int(data.get("exchange_id", 0)),
            additions=int(data.get("additions", 0)),
            deletions=int(data.get("deletions", 0)),
            input_tokens=int(data.get("input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
            cache_input_tokens=int(data.get("cache_input_tokens", 0)),
            duration=float(data.get("duration", 0.0)),
            compacted=bool(data.get("compacted", False)),
        )


def checkpoint_index(checkpoint: Checkpoint) -> dict:
    """历史里只存变更索引：回合、路径、增删行数、时间；正文走 get_diff。"""
    return {
        "exchange_id": checkpoint.exchange_id,
        "files": list(checkpoint.files),
        "additions": checkpoint.additions,
        "deletions": checkpoint.deletions,
        "created_at": checkpoint.created_at,
    }


def _exclude_spec(pattern: str) -> str:
    return pattern if pattern.startswith(":(exclude)") else f":(exclude){pattern}"


def _spawn_kwargs() -> dict[str, int]:
    """后台进程无需控制台，避免每轮 git 调用弹出 cmd 窗口。"""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


class SnapshotStore:
    """一个工作区对应一个影子 Git 仓库和一份 checkpoint 列表。"""

    def __init__(
        self,
        workspace: str | os.PathLike[str],
        *,
        root: str | os.PathLike[str] | None = None,
        keep: int = 50,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        base = (
            Path(root)
            if root is not None
            else ROOT / CONFIG.get("snapshot_dir", "data/snapshots")
        )
        key = hashlib.sha1(
            str(self.workspace).encode("utf-8")
        ).hexdigest()[:16]
        self._git_dir = base / key
        self._checkpoint_file = self._git_dir / "checkpoints.json"
        self._keep = keep
        self._git = shutil.which("git")
        self.available = bool(self._git)
        self._lock = asyncio.Lock()
        patterns = list(CONFIG.get("snapshot_exclude", DEFAULT_EXCLUDES))
        patterns.extend(MANDATORY_EXCLUDES)
        self._excludes = [
            _exclude_spec(pattern)
            for pattern in dict.fromkeys(patterns)
        ]
        if self.available:
            self._init_repo()
        self.checkpoints = self._load()

    # ------------------------------------------------------------ Git 基础
    def _init_repo(self) -> None:
        try:
            self._git_dir.mkdir(parents=True, exist_ok=True)
            if (self._git_dir / "HEAD").exists():
                return
            result = subprocess.run(
                [self._git, "init", "--bare", "--quiet", str(self._git_dir)],
                capture_output=True,
                text=True,
                timeout=30,
                **_spawn_kwargs(),
            )
            if result.returncode != 0:
                self.available = False
        except (OSError, subprocess.SubprocessError):
            self.available = False

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "GIT_DIR": str(self._git_dir),
                "GIT_WORK_TREE": str(self.workspace),
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        return env

    async def _run(
        self,
        *args: str,
        stdin: bytes | None = None,
    ) -> tuple[int, bytes, bytes]:
        if not self._git:
            raise SnapshotError("git is not installed")
        process = await asyncio.create_subprocess_exec(
            self._git,
            *args,
            cwd=str(self.workspace),
            env=self._env(),
            stdin=(
                asyncio.subprocess.PIPE
                if stdin is not None
                else asyncio.subprocess.DEVNULL
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_spawn_kwargs(),
        )
        try:
            out, err = await process.communicate(stdin)
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        return process.returncode or 0, out, err

    async def _snapshot_unlocked(self) -> str | None:
        # 快速检测：如果工作区无变化，直接用 write-tree 返回缓存的 tree
        # 注意不能加 -uno：未跟踪文件同样需要先 add 才能进入 tree
        status_code, status_out, _ = await self._run("status", "--porcelain")
        if status_code == 0 and not status_out.strip():
            # 索引无变化，尝试直接 write-tree（比 add -A 快得多）
            code, out, _err = await self._run("write-tree")
            if code == 0:
                tree = out.decode("utf-8", errors="replace").strip()
                if tree:
                    return tree
        code, _out, _err = await self._run(
            "add", "-A", "--", ".", *self._excludes
        )
        if code != 0:
            return None
        code, out, _err = await self._run("write-tree")
        if code == 0:
            return out.decode("utf-8", errors="replace").strip() or None
        # 空工作区：write-tree 可能报错，用空 tree 兜底
        code, out, _err = await self._run("mktree", stdin=b"")
        if code == 0:
            return out.decode("utf-8", errors="replace").strip() or None
        return None

    async def snapshot(self) -> str | None:
        """抓取当前工作区，返回 tree ID；失败返回 None。"""
        if not self.available:
            return None
        async with self._lock:
            return await self._snapshot_unlocked()

    async def changed_files(self, before: str, after: str) -> list[str]:
        """两次快照之间发生变化的文件（相对路径）。"""
        if not self.available:
            return []
        async with self._lock:
            return await self._changed_files_unlocked(before, after)

    async def _changed_files_unlocked(
        self,
        before: str,
        after: str,
    ) -> list[str]:
        code, out, _err = await self._run(
            "diff", "--name-only", "-z", before, after
        )
        if code != 0:
            return []
        return [
            os.fsdecode(item) for item in out.split(b"\0") if item
        ]

    async def diff_stat(self, before: str, after: str) -> tuple[int, int, int]:
        """两次快照之间的 (新增行, 删除行, 文件数)。"""
        if not self.available:
            return 0, 0, 0
        async with self._lock:
            code, out, _err = await self._run(
                "diff", "--numstat", before, after
            )
        if code != 0:
            return 0, 0, 0
        additions = deletions = files = 0
        for line in out.decode("utf-8", errors="replace").splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            files += 1
            if parts[0].isdigit():
                additions += int(parts[0])
            if parts[1].isdigit():
                deletions += int(parts[1])
        return additions, deletions, files

    async def diff_text(
        self,
        before: str,
        after: str,
        *,
        max_lines: int = 400,
    ) -> str:
        """两次快照之间的 unified diff（超出上限时截断）。"""
        if not self.available:
            return ""
        async with self._lock:
            code, out, _err = await self._run(
                "diff", "--no-color", "--unified=3", before, after
            )
        if code != 0:
            return ""
        lines = out.decode("utf-8", errors="replace").splitlines()
        if len(lines) > max_lines:
            hidden = len(lines) - max_lines
            lines = lines[:max_lines] + [f"… {hidden} more line(s)"]
        return "\n".join(lines)

    # ------------------------------------------------------------ 恢复
    async def restore(self, target: str) -> list[str]:
        """把工作区恢复到 ``target`` tree，返回受影响的文件。"""
        if not self.available:
            raise SnapshotError("git is not installed")
        async with self._lock:
            current = await self._snapshot_unlocked()
            if current is None:
                raise SnapshotError("could not snapshot the current workspace")
            if current == target:
                return []
            code, out, err = await self._run(
                "diff",
                "--name-status",
                "--no-renames",
                "-z",
                current,
                target,
            )
            if code != 0:
                raise SnapshotError(
                    err.decode("utf-8", errors="replace").strip()
                    or "git diff failed"
                )
            entries = self._parse_name_status(out)
            checkout = [
                path for status, path in entries if status in {"A", "M", "T"}
            ]
            deletions = [
                path for status, path in entries if status == "D"
            ]
            if checkout:
                code, _out, err = await self._run(
                    "checkout", target, "--", *checkout
                )
                if code != 0:
                    raise SnapshotError(
                        err.decode("utf-8", errors="replace").strip()
                        or "git checkout failed"
                    )
            for path in deletions:
                self._delete_file(path)
            return [path for _status, path in entries]

    @staticmethod
    def _parse_name_status(raw: bytes) -> list[tuple[str, str]]:
        parts = raw.split(b"\0")
        entries: list[tuple[str, str]] = []
        index = 0
        while index + 1 < len(parts):
            status = parts[index].decode("utf-8", errors="replace").strip()
            if not status:
                break
            entries.append((status[:1], os.fsdecode(parts[index + 1])))
            index += 2
        return entries

    def _delete_file(self, relative: str) -> None:
        path = (self.workspace / relative).resolve()
        if path != self.workspace and self.workspace not in path.parents:
            return
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            return
        parent = path.parent
        while parent != self.workspace and parent.is_dir():
            try:
                if any(parent.iterdir()):
                    break
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    # ------------------------------------------------------------ checkpoint
    def _load(self) -> list[Checkpoint]:
        if not self._checkpoint_file.is_file():
            return []
        try:
            data = json.loads(
                self._checkpoint_file.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return []
        checkpoints = []
        for item in data.get("checkpoints", []):
            try:
                checkpoints.append(Checkpoint.from_dict(item))
            except (KeyError, TypeError, ValueError):
                continue
        return checkpoints[-self._keep :]

    def save_checkpoints(self, checkpoints: list[Checkpoint] | None = None) -> None:
        if checkpoints is not None:
            self.checkpoints = checkpoints
        try:
            self._git_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "workspace": str(self.workspace),
                "checkpoints": [item.to_dict() for item in self.checkpoints],
            }
            tmp = self._checkpoint_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self._checkpoint_file)
        except OSError:  # 快照落盘失败不阻断本轮对话
            pass

    async def add_checkpoint(self, checkpoint: Checkpoint) -> list[Checkpoint]:
        async with self._lock:
            self.checkpoints.append(checkpoint)
            if len(self.checkpoints) > self._keep:
                self.checkpoints = self.checkpoints[-self._keep :]
            self.save_checkpoints()
            return list(self.checkpoints)

    def clear_checkpoints(self) -> None:
        self.checkpoints = []
        self.save_checkpoints()


def now_label() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
