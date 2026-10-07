"""一场会的 store：数据路径下面挂值。

一条**数据路径**就是 store 里的一条路（`case/quicksort/drafted`）。**值**就是那条路径下的那份文件
（`<算子>.viba`）：一次跑答下的东西，写成 viba 自己的快照——所以同一个调用再跑一遍读的就是这个值
（`replay` 这件事在 viba 那一层，`read_snapshot` / `write_snapshot`）。它不随工单走：工单里只有数据
路径和那次调用，要用的人照路径来读（见 `tickets.py`）。会议记录也在这份 store 旁边。

这个模块只管三件事：按数据路径读写值（含打包时抄一份）、记这场会的记录，外加"这条路径下有哪些值"。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Iterable, List, Tuple

from . import _viba  # noqa: F401

from viba.interpret import (Environment, EnvironmentCompute, EnvironmentStorage,
                            read_snapshot, write_snapshot)
from viba.reflect import access as reflect_access
from viba.type import VibaProgramErr

from .tickets import TICKETS_DIR

RECORD_HEADING = "# 这场会的记录"
VIBA_SUFFIX = ".viba"


def leaf_value(node) -> Any:
    """一个节点上那个值（一个叶子）；不是叶子、或者它写着 nil，就是 None。"""
    if node is None:
        return None
    given = reflect_access.leaf(node)
    if isinstance(given, VibaProgramErr) or given.ok_value is None:
        return None
    return given.ok_value


class CaseStorage(EnvironmentStorage):
    """A store that remembers which case it is running at.

    A ticket names no case: every data path in it is relative to the case, and
    the case is handed in when the run starts. Keeping it on the storage is what
    lets an operator turn one of those relative addresses into a full one — an
    operator gets its environment and nothing else.
    """

    __slots__ = ("case",)

    def __init__(self, cur_storage_path, sub_storage=None, store_root_dir=None,
                 case: str = ""):
        super().__init__(cur_storage_path, sub_storage, store_root_dir)
        self.case = case

    def sub(self, name: str) -> "CaseStorage":
        if name not in self.sub_storage:
            path = f"{self.cur_storage_path}/{name}" if self.cur_storage_path else name
            self.sub_storage[name] = type(self)(path, None, self.store_root_dir,
                                                self.case)
        return self.sub_storage[name]

    def absolute(self, module_path: str) -> str:
        """A ticket's data path, made absolute: "" is the case itself."""
        if not module_path:
            return self.case
        if module_path == self.case or (self.case and module_path.startswith(f"{self.case}/")):
            return module_path
        return f"{self.case}/{module_path}" if self.case else module_path

    def at(self, module_path: str) -> "CaseStorage":
        """Another data path of the same store, with the same case."""
        return type(self)(module_path, None, self.store_root_dir, self.case)


class Store:
    """一场会的那一份数据：`root` 是 store 的根目录。"""

    def __init__(self, root, record_path=None):
        self.root = Path(root)
        self.record_path = Path(record_path) if record_path is not None else None

    def env(self, module_path: str, viba_path=None, get_func=None, case: str = "") -> Environment:
        """The environment of one data path: its storage is `<root>/<module_path>`.

        `get_func(module, name)` is this run's compute side (see
        `host.Runner.get_func`); read-only callers leave it out.
        """
        return Environment(
            CaseStorage(module_path, None, str(self.root), case),
            EnvironmentCompute(get_func), viba_path)

    def read(self, module_path: str, name: str) -> Tuple[Any, bool]:
        """(留下的那个值, 答了没有)。值就是那条数据路径下 `<算子>.viba` 里那份。

        "答了没有"看的是那份值在不在——答下的东西不是一个叶子（一列、一份积）也算答了，只是这里给不出
        一个裸值来。
        """
        stored = read_snapshot(self.env(module_path), name)
        return leaf_value(stored), stored is not None

    def answered(self, module_path: str, name: str) -> bool:
        """这条数据路径下的这一步答了没有：那份值在不在。"""
        return read_snapshot(self.env(module_path), name) is not None

    def value(self, module_path: str, name: str) -> Any:
        """答下的那个值（一个叶子）；没答过、或者答下的不是叶子，就是 None。"""
        return leaf_value(read_snapshot(self.env(module_path), name))

    def write(self, module_path: str, name: str, value) -> None:
        """答一步：值落在这条数据路径下，写成 `<算子>.viba`（同一个调用再跑就是回放它）。"""
        write_snapshot(self.env(module_path), value, name)

    # ------------------------------------------------------------------
    # Which data paths carry something
    # ------------------------------------------------------------------

    def stored(self) -> List[Tuple[str, str]]:
        """这份 store 里留下的值：`(数据路径, 算子)`，按路径排（工单那几份不算）。"""
        found: List[Tuple[str, str]] = []
        if not self.root.is_dir():
            return found
        for file in sorted(self.root.rglob(f"*{VIBA_SUFFIX}")):
            parts = file.relative_to(self.root).parts
            if TICKETS_DIR in parts:
                continue
            found.append(("/".join(parts[:-1]), file.name[: -len(VIBA_SUFFIX)]))
        return found

    def copy_stored(self, into, wanted: Iterable[Tuple[str, str]]) -> List[Tuple[str, str]]:
        """Copy those (data path, definition) stored values into another store.

        This is what a ticket's bundle carries: the program plus the data it
        reads. Answers to steps are stored values too, so the same call copies a
        finished step along.
        """
        into = Path(into)
        copied: List[Tuple[str, str]] = []
        for module_path, name in wanted:
            source = self._stored_file(module_path, name)
            if source is None:
                continue
            target = into / self._relative(module_path, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append((module_path, name))
        return copied

    def _stored_file(self, module_path: str, name: str):
        path = self.root / self._relative(module_path, name)
        return path if path.is_file() else None

    @staticmethod
    def _relative(module_path: str, name: str) -> Path:
        parts = [part for part in str(module_path).split("/") if part]
        return Path(*parts) / f"{name}{VIBA_SUFFIX}"

    # ------------------------------------------------------------------
    # The meeting record
    # ------------------------------------------------------------------

    def records(self) -> List[str]:
        """这场会的记录，一行一行。"""
        if self.record_path is None or not self.record_path.exists():
            return []
        return self.record_path.read_text(encoding="utf-8").splitlines()

    def record(self, line: str) -> None:
        """Append one line to this meeting's record."""
        if self.record_path is None:
            return
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.record_path.exists():
            self.record_path.write_text(RECORD_HEADING + "\n", encoding="utf-8")
        with self.record_path.open("a", encoding="utf-8") as handle:
            handle.write(line.rstrip("\n") + "\n")
