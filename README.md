# gongtou（工头）

一个 Claude Code skill：**Claude 当工头，Codex 当工人。**

你照常跟 Claude 说想做什么。小改动 Claude 直接动手；大一点的活，Claude 会拆成几张任务卡派给 Codex，各自在独立分支里并行开发。每张卡做完都要过测试和审查，Claude 验收、合并后再整体审一遍，通过了还能自动部署到你的服务器。

你只管提需求和拍板，分活、盯进度、验收、合并、上线都交给工头。

## 安装

需要：Linux、Python 3.11+、Git、[Claude Code](https://claude.com/claude-code)、[Codex CLI](https://github.com/openai/codex)

```sh
git clone https://github.com/lazitech/gongtou.git
cd gongtou
./install.sh --with-skills
```

## 配置

在你的项目目录里打开 Claude Code，说一句：

> /gongtou 帮我配置

Claude 会自己找出项目的测试命令，跑一遍确认能用，然后只问你一个问题：**要不要部署到服务器？**

- **不用**：只在本地开发、审查。
- **要**：从你的 SSH 配置里选一台服务器，确认后 Claude 写好部署设置。

全程不用手动改配置文件。

## 工作流程

```
你提需求
   │
Claude 估计工作量 ──── 小改动 ──→ Claude 直接改完
   │ 大改动
   ▼
Claude 写任务卡，派给 Codex（多张卡并行）
   │
每张卡：Codex 开发 → 跑测试 → Codex 审查
   │        └─ 有问题：Claude 核实后打回原工人修改
   ▼
Claude 合并所有通过的卡，整体再审一遍
   │
部署到服务器（如果配置了）→ 服务器上跑测试 → 推送到 GitHub
```

## 常用说法

| 你说 | 会发生什么 |
|---|---|
| `/gongtou 加一个导出功能` | 按上面的流程派卡开发 |
| 你自己做 | Claude 直接改，不派卡 |
| 帮我配置 | 初始化或修改项目配置 |
| 合并上线 | 合并已通过的卡，整体审查并部署 |

## 放心用

- Codex 干活时读不到你的 SSH 私钥、各种登录凭据和 `.env` 文件。
- 部署前会确认服务器空闲、代码干净；任何一步失败就停下，保留现场。
- 用 GPU 或付费服务的任务，每次都要你点头。

---

想了解实现细节，看 [技术参考](docs/reference.md)。MIT 许可。
