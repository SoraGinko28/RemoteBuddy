"""一个案子的值：数据路径下面挂值。

一条**数据路径**就是案子里面的一条路（`""` 是案子本身，`"revised"` 是它下面那一层）。**值**就是那条
路径下的那份文件（`<算子>.viba`）：一次跑答下的东西，写成 viba 自己的快照——所以同一个调用再跑一遍
读到的就是它（这件事在 viba 那一层：`get_snapshot` / `put_snapshot`）。

值不随工单走：单上只有那次调用和它跑在的那个案子的根，要用的人照数据路径来读。这个模块只管按路径读
写值，外加"这条路径下有哪些值"和这场会的记录。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Tuple

from . import _viba  # noqa: F401

from viba.interpret import (Environment, EnvironmentCompute, EnvironmentStorage,
                            get_snapshot, put_snapshot)
from viba.reflect import access as reflect_access
from viba.type import VibaProgramErr

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


class Store:
    """一个案子的那份数据：`root` 是这个案子值的目录。"""

    def __init__(self, root, record_path=None):
        self.root = Path(root)
        self.record_path = Path(record_path) if record_path is not None else None

    def env(self, module_path: str, viba_path=None, get_func=None) -> Environment:
        """一条数据路径的环境：它的存储根是这个案子，路径就是这条数据路径。

        `get_func(module, name)` 是这次跑的算子来源（见 `binding.Compute`）；只读的人不给它。
        """
        return Environment(EnvironmentStorage(module_path, None, str(self.root)),
                           EnvironmentCompute(get_func), viba_path)

    def read(self, module_path: str, name: str) -> Tuple[Any, bool]:
        """(留下的那个值, 答了没有)。值就是这条数据路径下 `<算子>.viba` 里那份。

        "答了没有"看的是那份值在不在——答下的东西不是一个叶子（一列、一版代码）也算答了，只是这里
        给不出一个裸值来。
        """
        stored = get_snapshot(self.env(module_path), name)
        return leaf_value(stored), stored is not None

    def answered(self, module_path: str, name: str) -> bool:
        """这条数据路径下的这一步答了没有：那份值在不在。"""
        return get_snapshot(self.env(module_path), name) is not None

    def value(self, module_path: str, name: str) -> Any:
        """答下的那个值（一个叶子）；没答过、或者答下的不是叶子，就是 None。"""
        return leaf_value(get_snapshot(self.env(module_path), name))

    def write(self, module_path: str, name: str, value) -> None:
        """答一步：值落在这条数据路径下，写成 `<算子>.viba`（同一个调用再跑就是回放它）。"""
        put_snapshot(self.env(module_path), value, name)

    # ------------------------------------------------------------------
    # 这里有哪些值
    # ------------------------------------------------------------------

    def stored(self) -> List[Tuple[str, str]]:
        """这个案子里留下的值：`(数据路径, 算子)`，按路径排。"""
        found: List[Tuple[str, str]] = []
        if not self.root.is_dir():
            return found
        for file in sorted(self.root.rglob(f"*{VIBA_SUFFIX}")):
            parts = file.relative_to(self.root).parts
            found.append(("/".join(parts[:-1]), file.name[: -len(VIBA_SUFFIX)]))
        return found

    # ------------------------------------------------------------------
    # 这场会的记录
    # ------------------------------------------------------------------

    def records(self) -> List[str]:
        """这场会的记录，一行一行。"""
        if self.record_path is None or not self.record_path.exists():
            return []
        return self.record_path.read_text(encoding="utf-8").splitlines()

    def record(self, line: str) -> None:
        """往这场会的记录里追加一行。"""
        if self.record_path is None:
            return
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.record_path.exists():
            self.record_path.write_text(RECORD_HEADING + "\n", encoding="utf-8")
        with self.record_path.open("a", encoding="utf-8") as handle:
            handle.write(line.rstrip("\n") + "\n")
