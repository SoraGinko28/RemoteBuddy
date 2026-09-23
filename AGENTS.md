# AGENTS.md

## Git 流程（用户明确要求，长期有效）
- **没有用户明确的“提 PR / pr 到 wtj / 给我链接”指令，绝不创建任何 PR。**
- **没有用户明确说“push / 推上去”，绝不 push（origin 也不许）。** 默认动作只有：建分支 → 合并到本地 main。到此为止。
- 只有用户亲口说提 PR 时才开 PR，且一个 PR 只带一个 commit。
- **用户说“pr”时：开（或更新）PR 之后直接 merge**，只要 token 有权限，不要再问第二遍。
  - `gh pr merge` / `gh pr edit` 走 GraphQL，只有 `repo` 权限的 token 会被挡；改用 REST：`PUT /repos/<owner>/<repo>/pulls/<n>/merge`、`PATCH /repos/<owner>/<repo>/pulls/<n>`。
- 不要擅自把分支推到 wtj，不要擅自合并到 wtj。
- 讨论阶段（ADT、记忆文件等）的改动默认只留在本地。

## 讨论 ADT 时（用户明确要求，长期有效）
- 只谈 ADT 层：类型、字段、身份、`resolve` 之类的谓词、Assert / Appendix / Hint。
- **不要扯实现层细节**（服务端怎么校验、前端怎么渲染、`text_of` 这类函数），除非用户主动问实现。
- 加进 Meeting ADT 的概念必须先在 ADT 里有位置，不能拿存储格式或代码结构当语义原语。

## 展示改动（用户明确要求，长期有效）
- 每次改了文件，**主动**用 `git diff` / `git show` 把改动贴出来，不要只用文字说“改了什么”。
- 不要等用户开口要 diff。

## 用词（用户明确要求，长期有效）
- **“散文”这两个字禁止使用**：任何场合都不许说（拿它称呼 `Assert` / `Appendix` / `Hint` 的实参尤其不许），也不许换成英文说。
- 说 `Assert` / `Appendix` / `Hint` 就直接写名字，不给它们另编类别词；要指它们的实参，就说是 code block。

## 文件放哪（用户明确要求，长期有效）
- **跟本项目无关的文件**（给上游的报告、临时笔记、复现脚本等）**不要放进仓库**，放到仓库外，比如 `~/workspace/` 下。
- 仓库里只留项目自己的东西。

## 本地那个页面（教训，长期有效）
- 8765 的页面是**长驻进程**，内存里是它启动那一刻的代码：改角色名、改 manifest 字段这类东西之后必须重启它，否则它拿旧校验把新数据判成非法——列表里会凭空少掉一批会议（这次把角色改成 `actor`，77 场会议就从列表里消失了）。
- 重启：`kill <旧 PID>`，仓库根下 `nohup .venv/bin/python web_v2.py --harness=trae > /tmp/web_v2_page.log 2>&1 &`，再确认 `curl -s localhost:8765/api/meetings` 的 `warnings` 是空的。
