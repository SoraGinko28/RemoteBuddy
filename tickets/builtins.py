"""viba 自带的那些算子，由一个宿主答（`viba/builtin.viba` 里声明的那一份）。

viba 自己不带库：一段程序能调到的实现全在宿主的 compute 那一侧，`builtin.viba` 里那些算术、比较、
字符串也一个都不例外。处理一张单的进程就是一个宿主，所以它得答得上来——不然规范里的程序连两个数相加
都没有东西可用。

这里就是那一份：`int`、`float`、`str`、`bool` 上的算术、比较和转换，一个成员一个实现；名字和意思都
是 `viba/builtin.viba` 那个文件的，不是这一处的。一个成员自己的名字和整名是同一个调用（`add << …`
就是 `builtin.add << …`，所以两种写法都在这张表里），而角色自己那张表里的实现盖过这里。
"""


def _int(one) -> int:
    return int(one.value)


def _float(one) -> float:
    return float(one.value)


def _str(one) -> str:
    return str(one.value)


def _bool(one):
    return one.value if hasattr(one, "value") else one


# {name: function(environ, x): value} and {name: function(environ, x, y): value}.
_ONE = {
    # `$echo` 把收到的原样交回（`builtin.viba` 的第一项）。
    "$echo": lambda _e, x: x,
    "$neg": lambda _e, x: -_int(x),
    "$abs": lambda _e, x: abs(_int(x)),
    "$neg_f": lambda _e, x: -_float(x),
    "$abs_f": lambda _e, x: abs(_float(x)),
    "$not": lambda _e, x: not _bool(x),
    "$len_str": lambda _e, x: len(_str(x)),
    "$upper_str": lambda _e, x: _str(x).upper(),
    "$lower_str": lambda _e, x: _str(x).lower(),
    "$int_to_float": lambda _e, x: float(_int(x)),
    "$float_to_int": lambda _e, x: int(_float(x)),
    "$int_to_str": lambda _e, x: str(_int(x)),
    "$float_to_str": lambda _e, x: str(_float(x)),
    "$bool_to_str": lambda _e, x: "true" if _bool(x) else "false",
    "$str_to_int": lambda _e, x: int(_str(x)),
    "$str_to_float": lambda _e, x: float(_str(x)),
}

_TWO = {
    "$add": lambda _e, x, y: _int(x) + _int(y),
    "$sub": lambda _e, x, y: _int(x) - _int(y),
    "$mul": lambda _e, x, y: _int(x) * _int(y),
    "$div": lambda _e, x, y: _int(x) // _int(y),
    "$rem": lambda _e, x, y: _int(x) % _int(y),
    "$min": lambda _e, x, y: min(_int(x), _int(y)),
    "$max": lambda _e, x, y: max(_int(x), _int(y)),
    "$pow": lambda _e, x, y: _int(x) ** _int(y),
    "$lt": lambda _e, x, y: _int(x) < _int(y),
    "$le": lambda _e, x, y: _int(x) <= _int(y),
    "$gt": lambda _e, x, y: _int(x) > _int(y),
    "$ge": lambda _e, x, y: _int(x) >= _int(y),
    "$eq": lambda _e, x, y: _int(x) == _int(y),
    "$ne": lambda _e, x, y: _int(x) != _int(y),
    "$add_f": lambda _e, x, y: _float(x) + _float(y),
    "$sub_f": lambda _e, x, y: _float(x) - _float(y),
    "$mul_f": lambda _e, x, y: _float(x) * _float(y),
    "$div_f": lambda _e, x, y: _float(x) / _float(y),
    "$rem_f": lambda _e, x, y: _float(x) % _float(y),
    "$min_f": lambda _e, x, y: min(_float(x), _float(y)),
    "$max_f": lambda _e, x, y: max(_float(x), _float(y)),
    "$pow_f": lambda _e, x, y: _float(x) ** _float(y),
    "$lt_f": lambda _e, x, y: _float(x) < _float(y),
    "$le_f": lambda _e, x, y: _float(x) <= _float(y),
    "$gt_f": lambda _e, x, y: _float(x) > _float(y),
    "$ge_f": lambda _e, x, y: _float(x) >= _float(y),
    "$eq_f": lambda _e, x, y: _float(x) == _float(y),
    "$ne_f": lambda _e, x, y: _float(x) != _float(y),
    "$concat": lambda _e, x, y: _str(x) + _str(y),
    "$eq_str": lambda _e, x, y: _str(x) == _str(y),
    "$ne_str": lambda _e, x, y: _str(x) != _str(y),
    "$lt_str": lambda _e, x, y: _str(x) < _str(y),
    "$le_str": lambda _e, x, y: _str(x) <= _str(y),
    "$gt_str": lambda _e, x, y: _str(x) > _str(y),
    "$ge_str": lambda _e, x, y: _str(x) >= _str(y),
    "$and": lambda _e, x, y: _bool(x) and _bool(y),
    "$or": lambda _e, x, y: _bool(x) or _bool(y),
    "$xor": lambda _e, x, y: _bool(x) != _bool(y),
    "$eq_bool": lambda _e, x, y: _bool(x) == _bool(y),
    "$ne_bool": lambda _e, x, y: _bool(x) != _bool(y),
}


def the_table() -> dict:
    """每一个算子，按它那几种写法都放一份。

    `builtin.mul` 是 `get_func` 问的那个整名，`mul` 是成员自己的名字，`$mul` 是写下来的那种写法——
    `viba/builtin.viba` 说它们是同一个调用，所以一张表把三种都答上。
    """
    table = {}
    for name, one in list(_ONE.items()) + list(_TWO.items()):
        table[name] = one
        table[name[1:]] = one
        table[f"builtin.{name[1:]}"] = one
    return table
