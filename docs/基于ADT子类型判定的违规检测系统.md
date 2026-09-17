# 基于 ADT 子类型判定的违规检测系统

> 技术原理文档 · 2026-09-14
> 术语与机制已按当前 `viba` 规则层（`viba-rule.md`）对齐：`Predicate` / `PredicationFailed` / `is_compliant` / `is_shape_compatible` / `reset_predication_by_python_code`。

---

## 1. 我们要解决什么问题

我们在做一个大模型驱动的约束检查：给一段材料（代码、文档），让大模型找出其中不合规的地方。整个问题分三层：

- **Metrics**：带语义的基础类型（bool / int / float / str），比如"函数代码行数"。
- **Rules**：用 Viba ADT 写的规则，叶子是 Metrics、字面量和 `Predicate` 谓词。
- **Witness**：把 Rule 的叶子换成材料里取到的真实值，也就是"证据"。

流程是这样的：规则实现方给定 Rules；大模型先猜材料违反了哪些规则，再照着规则去材料里取证——把每个叶子 metric 的实际数值填进去，得到一个 Witness；每个断言的谓词代码跑一遍，决定那个叶子的最终写法。

于是核心问题就一句话：**能不能用子类型判定 `Witness0 <: Rule0` 来表达"这条证据满足这条规则"？**

答案：能。判定就是 `is_compliant(witness, rule)`，它是对 `is_sub_type` 的薄包装，不剥任何东西、不求任何值。另有 `is_shape_compatible(witness, rule)` 负责"形状准备好没有"——它把两边的谓词叶子都擦成同一个毒剂再比 `<:`。

---

## 2. Viba 是什么

Viba 是一个用代数运算写类型的 DSL（PLY 实现，见 `viba/parser.py`）。跟本系统相关的构造就这些：

| 运算 | 写法 | 意思 |
|------|------|------|
| 和（Sum） | `A \| B` | 或 |
| 积（Product） | `A * B` | 与 |
| 指数（Exponent） | `B <- A` | 函数类型 A → B |
| 语义标签 | `$label T` | 命名字段 / 变体，支持点分路径（`$meta.id.hash`） |
| 单位元 | `void` | 积的单位：`A * void ≡ A` |
| 底 | `never` | 和的单位：`A \| never ≡ A` |
| 省略 | `...` | 开放类型，"这部分我不关心" |
| 元组 | `(A, B, C)` | `A * B * C` 的语法糖 |
| 代码块 | `{ ... }` | 任意文本，支持嵌套，整体吞入 |

优先级从低到高：`|` < `*` < `<-`。字面量（int / float / 三种引号的 string / bool）和类型引用是最底层的原子。

规则层用到的词汇都定义在 `viba/builtin.viba`，它们不是新机制，只是类型写法：

```
Predicate[Cond, PythonCode] := $assert_cond Cond * $assert_python_code PythonCode
Metric[T] := T
PredicationFailed[T, MsgStr] := $__assertion_failed_original_data T * $__assertion_failed_error_msg MsgStr
not[A] := never <- A
```

`Predicate` 的第一个参数是条件的自然语言描述，不参与执行；第二个参数 `$python_code {...}` 才是可执行的判定器。规则定义以 `RuleObject`（同义于 `Object`）或 `OneofRule`（同义于 `Oneof`）起头——标记只是声明"这是一个规则"，一个 module 里只有带标记的定义参与规则枚举。

---

## 3. 整体架构

```
                ┌──────────────────────────┐
                │  llm-as-compiler         │
  Rule0 ───────►│  编译出 predicator：       │──► 每个 Predicate 的 $python_code
 (含 Predicate  │  predicate(self) 返回     │    (生成一次，反复执行)
  叶子)         │  断言成立与否              │
                │  （规则本身保持原样，      │
                │    谓词叶子留在 Rule 里）  │
                └──────────────────────────┘

  材料 ──► LLM 取证 ──► Witness
           (骨架照抄 Rule，metric 叶子填实测值)

  取证期执行 predicator：reset_predication_by_python_code(witness)
        └─ 断言成立 → 叶子照写 Predicate[...]
           断言不成立 → 叶子换成 PredicationFailed[nil, str]

  判定内核（纯结构，无环境，无求值）：
        is_compliant(witness, rule)      = witness <: rule
        is_shape_compatible(witness, rule) = 擦掉谓词叶子之后比 <:
        lint：Rule 侧不得出现 PredicationFailed
```

注意分工：**大模型只做两件需要"理解材料"的事——写规则的谓词代码、在材料里取证**。其中取证的"判定"环节（值合不合规）并不是大模型拍脑袋，而是执行谓词代码——是确定性的。到了判定内核，连代码都不执行了，就是纯语法递归。所有不确定性都收在大模型侧，内核可审计、可缓存、可回归测试。

---

## 4. 这个设计是怎么一步步走到这里的

### 4.1 直接判定 `Witness0 <: Rule0`：叶子上的约束表达不了

先想清楚标准 ADT 子类型能干什么。对结构它确实够用：和的宽度子类型（证据命中某个变体就行）、积的逐字段判定、指数的参数逆变返回协变、`...` 直接放行——这些都能机械地查。

问题出在叶子。假设规则想说"行数 ≤ 100"，结构上它就是 `$lines int`。把字面量看成单例类型，`250 <: int` 和 `42 <: int` **一样成立**——任何数字都是 int。子类型回答的是"这个值是不是这个类型的居民"，可我们想要的是"这个值满不满足 ≤ 100"。阈值这种东西，`| * <-` 三个运算表达不了——它们管形状，不管大小。

### 4.2 把约束变成节点：`Predicate` 进入 ADT

第一版修正：把约束提升成积里的一个带标签字段——

```
$x int * $check_x Predicate[{x <= 100}, $python_code {
def predicate(self):
    return self.x.value <= 100
}]
```

这里有个坑必须躲开：如果 Witness 照着 Rule 的样子**把 `Predicate` 叶子原样抄过来**，那这个分支的比较就变成了 `Predicate[...] <: Predicate[...]`，而两边逐字段相同，自反，恒真——约束被"抄作业"这个动作悄悄吃掉了。

### 4.3 最终版：求值留在内核之外

解法不是让判定器去求值，而是**别让判定器碰求值**。三件事各归各位：

- **谓词代码在取证期执行**：`reset_predication_by_python_code(witness)` 把每个 `Predicate` 的 `$python_code` 跑一遍，`self` 由带标签字段拼出来（标签去掉 `$` 前缀，metric 的实测值取 `.value`），返回假的那个叶子就地换成 `PredicationFailed[nil, str]`。
- **判定内核只做结构递归**：`is_compliant(witness, rule) = witness <: rule`，不看环境、不执行代码。抄不抄作业不再重要——合法的抄写只可能发生在谓词代码已经点头之后。
- **形状检查另走一条**：`is_shape_compatible(witness, rule)` 把两边的 `Predicate[...]` 与 `PredicationFailed[...]`（包括藏在类型名后面的，先展开）都擦成同一个毒剂叶子，再比 `witness <: rule`。它只回答"形状准备好了没有"：字段不能缺、tag 不能错、度量的数据类型也要对，但断言成没成不影响。

（9/14 的版本曾把 `Assert` 在编译期折成一个名义类型名，比如 `IntLessThan100 := $x int`，让约束消失进名字里。现在不这么走：谓词叶子留在 Rule 里，判定始终是 `witness <: rule`，缩掉的那一步只保留在"形状检查"里。）

### 4.4 毒叶：为什么不能用 `never`，要用 `PredicationFailed`

取证时怎么标记"这个叶子违规了"？最先想到借 `never`：

- x = 50（合规）：写 `$x 50`；
- x = 101（违规）：写 `$x never`。

然后指望 `50 <: int` 为真、`never <: int` 为假。**但标准子类型里 `never <: int` 恰恰为真**：`never` 是和类型的单位元，就是底类型 ⊥，而 ⊥ 是任何类型的子类型（它没有居民，"它的居民都是 int 的居民"这句话空泛地成立）。真这么判，违规反而能过——判定方向反了。

修正：自定义一个毒剂 `PredicationFailed[T, MsgStr]`。它就是普通的命名泛型，跟 `Predicate` 没有任何子类型关系，`PredicationFailed[nil, str] <: Predicate[...]` 自然为假。三条约定：

1. **不透明**：没有展开式，只有名字完全相同才匹配；
2. **只许出现在 Witness 侧**：Rule 里出现直接 lint 报错——它是取证标记，不是类型语言的一部分；
3. **位置不用特殊处理**：积里任何一个是它，整个积挂；和里某一支是它，那一支不命中；指数两边是它也都挂（参数位逆变之后还是要 `int <: PredicationFailed[...]`，还是假）。标准递归天然全覆盖。

直觉是这样的：Witness 是在**声称"我有一个值"**（证据），毒叶声称的是"我没有值"，而无物的声称不算合规。禁止性规则里的用法是同一个道理：`not[A] := never <- A`，Witness 保持 `not[oneof]` 外壳，把每个分支的叶子换成毒剂，Rule 那边才收得下。

---

## 5. 判定规则

记 Rule 为 T，Witness 为 R，都是 Viba ADT。判定 `R <: T` 就是结构递归：

**叶子**

```
Literal(v) <: TypeRef(m)              ⟺  v 是 m 的合法取值（int/float/str/bool 型检）
Literal(v) <: Literal(v)              ⟺  恒真
Predicate[...] <: Predicate[...]      ⟺  恒真（两边叶子逐字段相同：条件是自然语言、代码是代码块）
PredicationFailed[...] <: T           ⟺  恒假（毒叶，对任何 T 都假）
never <: never                        ⟺  真（Rule 自身的 never 结构被原样抄过来，不误伤）
```

**复合结构**

```
R <: T1 | T2        ⟺  R <: T1  ∨  R <: T2        （证据落一支就行）
R1 * R2 <: T1 * T2  ⟺  R1 <: T1 ∧ R2 <: T2        （一支毒全锅端）
R <- R' <: T <- T'  ⟺  R <: T ∧ T' <: R'          （返回协变，参数逆变）
R <: ...            ⟺  恒真（规则不关心的区域随便填）
```

**前置 lint**

```
Rule 侧含 PredicationFailed  ⇒  拒绝
Predicate 只允许以 `$tag Predicate[{...}, $python_code {...}]` 的形态出现在积位置
$python_code 代码块内花括号必须成对（CodeBlock 词法约束）
判定器入口函数名必须是 predicate，恰好一个参数，约定叫 self
```

**完备性**：合规材料每个叶子都满足 metric 类型、每个断言都被代码判为成立，结构递归合取/析取展开后整体为真；违规材料至少有一个叶子被换成毒叶，那次叶子比较为假，沿积的合取、和的析取往上传播，整体为假。判定既不冤枉好人，也不放过坏人。

**一个好用的结论（ground 定理）**：编译和取证已经把所有需要动脑子求值的东西都干完了，判定器看到的全是 ground 结构。这时：

```
R <: T   ⟺   把 R 叶的值代进 T 的布尔骨架里求值，结果为真
```

和对应析取、积对应合取、叶子型检对应原子命题——**这个子类型判定本质上就是一次布尔求值**。所以判定器可以写得非常薄：纯语法递归，不带环境，不执行任何代码。

---

## 6. 从 Curry-Howard 再看一遍

这套东西在逻辑里有干净的对应（Curry-Howard）：

| 逻辑 | 类型 | Viba |
|------|------|------|
| A ∧ B | 积 | `A * B` |
| A ∨ B | 和 | `A \| B` |
| A → B | 函数 | `B <- A` |
| ⊥（假） | 初始对象 | `never` |
| ⊤（真） | 终对象 | `void` |
| ¬A | A → ⊥ | `never <- A` |

Rule 就是命题，Witness 就是证据，判定就是 inhabitation——"这个命题有没有居民"。规则层里的 `not[A] := never <- A` 正是 ¬A := A → ⊥。

两点说明：

**这是直觉主义逻辑，不是经典逻辑。** 没有排中律（`A ∨ ¬A` 不自动成立），没有双否定消除，De Morgan 只成立构造性方向。落到系统里就是：规则写了 `$ok A | $err (never <- A)` 不算完，证据必须真的落在某一支——这跟我们"大模型负责给证据"的流程正好合拍，不算负担。

**直觉主义和经典逻辑的差别在我们这里被抹平了。** 差别的根源是不可判定的东西，而我们的判定器只碰已经求好值的原子，inhabitation 判定和布尔求值就是一回事（上一节的 ground 定理）。系统上头用命题的表达方式描述规则，下头用布尔求值的机械方式做判定，两头的好处都占。

**毒叶的逻辑含义**：`PredicationFailed` 就是**声称自己手里有 ⊥ 的证据**。可 ⊥ 按定义没有任何证据，所以这一声称本身就是证伪——手工实现了一句"ex falso 不可达"。

---

## 7. 可以拿去当测试的定律

下面几条应该在判定器上恒成立，直接做成属性测试：

1. **和单位元**：`A | never ≡ A`（两个方向都通）
2. **积单位元**：`A * void ≡ A`
3. **和的结合/交换**（判定层面，链式展平之后看）
4. **积的结合/交换**（同上，经 `chain.py` 展平/重组）
5. **De Morgan（直觉主义方向）**：`never <- (A | B)` 蕴含 `(never <- A) * (never <- B)`
6. **逆变保持**：`B <- A` 对 A 逆变、对 B 协变（替换引理）
7. **毒叶传播**：合法 R 的任一叶子换成 `PredicationFailed`，判定必为假
8. **省略号吸收**：`...` 区域随便填什么，判定结果不变
9. **形状与合规的强弱**：`is_shape_compatible` 成立是 `is_compliant` 成立的必要条件，反之不然；两者只在谓词叶子上有差别

---

## 8. 边界和约定一览

| 约定 | 为什么 |
|------|--------|
| `Predicate` 只作为积里的带标签字段 | 积的逐字段判定才有稳定的对位；和的分支里孤立一个谓词，擦除与判定的结果都无定义 |
| 积里不带标签的字段一律前置 | 带标签的按名字对位，不带标签的按位置对位；集中前置，位置对位的段落才有稳定顺序 |
| `PredicationFailed` 只许出现在 Witness 侧 | 它是取证标记，混进 Rule 就没法区分"规则禁止"和"证据说没有" |
| Witness 保留 Rule 的完整骨架，只把叶子换掉 | 保证结构递归两边分量对得上 |
| `not[...]` 外壳原样保留、分支叶子换成毒剂 | 少一个分支、或某个分支留成正向，都判 False |
| `$python_code` 花括号成对、入口 `predicate(self)` | CodeBlock 词法约束 + 判定器入口约定 |
| 指数位如果出现函数证据，函数体怎么查要另行约定 | ground 布尔退化管不了函数体内部；不过 predicator 已经编译出系统了，预计 Witness 里不会出现函数证据 |
| 和按"存在分支"读，不按"全称"读 | witness 语义：`never` 分支的意思就是"禁止落到这" |

**大模型会错在哪，内核抓哪类错**：幻觉出不存在的 metric、类型填错——结构层 `<:` 抓；数值真超了——毒叶抓。两类失败各有各的错误信息，好排查。

---

## 9. 一句话总结

> **判定式**：`is_compliant(witness, rule)`，即 `witness <: rule`，外加一条 lint（Rule 侧不得出现 `PredicationFailed`）。形状另由 `is_shape_compatible` 单独查，谓词的真假在取证期由 `reset_predication_by_python_code` 决定。
> 等效于在 ground 结构上做一次布尔求值；类型论里叫 refinement 子类型判定；Curry-Howard 的视角下就是"证据 inhabits 命题"。

这套设计最舒服的地方在时间维度上的分工：大模型负责写谓词代码和取证（要理解材料），生成的代码负责确定性的求证，判定内核只做纯结构递归（要的是可复现）。智能的事全在前面干完，后面全是机械活。
