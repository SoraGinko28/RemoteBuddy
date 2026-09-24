"""一场会的 store：数据路径下面挂材料。

一条**数据路径**就是 store 里的一条路（`case/quicksort/drafted`）。**材料**是那条路下面的一条
Prepare：`$call` 是这一步读了什么，`$measured` 是它答了什么（没答是 `nil`）。材料不随工单走——
工单里只有数据路径，要用的人照路径来读。会议记录也在这份 store 旁边。

这个模块只管两件事：按数据路径读写材料（含打包时抄一份），和记这场会的记录。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Iterable, List, Tuple

from . import _viba  # noqa: F401

from viba.compliance import (PreparedStorage, measured_of, prepare_path, read_prepare,
                             record_prepare)
from viba.interpret import Environment, EnvironmentCompute

RECORD_HEADING = "# 这场会的记录"
PREPARE_SEGMENT = "prepare"
VIBA_SUFFIX = ".viba"


class CaseStorage(PreparedStorage):
    """A store that remembers which case it is running at.

    A ticket names no case: every data path in it is relative to the case, and
    the case is handed in when the run starts. Keeping it on the storage is what
    lets an operator turn one of those relative addresses into a full one — an
    operator gets its environment and nothing else.
    """

    __slots__ = ("case",)

    def __init__(self, cur_storage_path, sub_storage=None, store_root_dir=None,
                 prepare_root_dir=None, case: str = ""):
        super().__init__(cur_storage_path, sub_storage, store_root_dir, prepare_root_dir)
        self.case = case

    def sub(self, name: str) -> "CaseStorage":
        if name not in self.sub_storage:
            path = f"{self.cur_storage_path}/{name}" if self.cur_storage_path else name
            self.sub_storage[name] = type(self)(path, None, self.store_root_dir,
                                                self.prepare_root_dir, self.case)
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
        return type(self)(module_path, None, self.store_root_dir, self.prepare_root_dir,
                          self.case)


class Store:
    """一场会的那一份数据：`root` 是 store 的根目录。"""

    def __init__(self, root, record_path=None):
        self.root = Path(root)
        self.record_path = Path(record_path) if record_path is not None else None

    def env(self, module_path: str, viba_path=None, get_func=None, case: str = "") -> Environment:
        """The environment of one data path: its storage is `<root>/<module_path>`.

        `get_func(module_path, func_name)` is this run's compute side (see
        `host.Runner.get_func`); read-only callers leave it out.
        """
        return Environment(
            CaseStorage(module_path, None, str(self.root), str(self.root), case),
            EnvironmentCompute(get_func), viba_path)

    def prepared(self, module_path: str, name: str):
        """The Prepare at that data path, or None when there is none."""
        return read_prepare(self.env(module_path), name)

    def read(self, module_path: str, name: str) -> Tuple[Any, bool]:
        """(the material, answered)."""
        return measured_of(self.prepared(module_path, name))

    def answered(self, module_path: str, name: str) -> bool:
        return self.read(module_path, name)[1]

    def write(self, module_path: str, name: str, value, call=None) -> None:
        """Answer one step: the material lands at that step's own data path."""
        record_prepare(self.env(module_path), name, call, value)

    # ------------------------------------------------------------------
    # Which data paths carry something
    # ------------------------------------------------------------------

    def materials(self) -> List[Tuple[str, str]]:
        """Every (data path, definition) this store carries, in path order."""
        found: List[Tuple[str, str]] = []
        if not self.root.is_dir():
            return found
        for file in sorted(self.root.rglob(f"*{VIBA_SUFFIX}")):
            parts = file.relative_to(self.root).parts
            if PREPARE_SEGMENT not in parts:
                continue
            index = parts.index(PREPARE_SEGMENT)
            found.append(("/".join(parts[:index]), parts[-1][: -len(VIBA_SUFFIX)]))
        return found

    def copy_materials(self, into, wanted: Iterable[Tuple[str, str]]) -> List[Tuple[str, str]]:
        """Copy those (data path, definition) materials into another store.

        This is what a ticket's bundle carries: the program plus the data it
        reads. Answers to steps are materials too, so the same call copies a
        finished step along.
        """
        into = Path(into)
        copied: List[Tuple[str, str]] = []
        for module_path, name in wanted:
            source = self._material_file(module_path, name)
            if source is None:
                continue
            target = into / self._relative(module_path, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append((module_path, name))
        return copied

    def _material_file(self, module_path: str, name: str):
        path = self.root / self._relative(module_path, name)
        return path if path.is_file() else None

    @staticmethod
    def _relative(module_path: str, name: str) -> Path:
        parts = [part for part in str(module_path).split("/") if part]
        return Path(*parts) / prepare_path(f"{name}{VIBA_SUFFIX}")

    # ------------------------------------------------------------------
    # The meeting record
    # ------------------------------------------------------------------

    def record(self, line: str) -> None:
        """Append one line to this meeting's record."""
        if self.record_path is None:
            return
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.record_path.exists():
            self.record_path.write_text(RECORD_HEADING + "\n", encoding="utf-8")
        with self.record_path.open("a", encoding="utf-8") as handle:
            handle.write(line.rstrip("\n") + "\n")
