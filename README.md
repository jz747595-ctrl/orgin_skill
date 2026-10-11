# orgin_skill · EditaPlot 分发版

这是一个**个人分发副本**：基于开源项目 [EditaPlot](https://github.com/hang-jin/editaplot)（Apache-2.0）
在 `0172103` 的快照，加上该快照之后我这边做的改进。**它不是上游的正式发布**，上面那个链接才是原项目。

用途很直接：把整份仓库交给一个 AI（或同事），对方 `git clone` 下来就能安装、自检、出图。

## 交给 AI 的一句话

> 克隆这个仓库，读 `docs/installation.md`。用 `.\editaplot.cmd setup --target "<你的 skill 目录>\editaplot"` 安装；
> 如果你的平台没有 skill 目录的概念，跳过 `setup`，直接在仓库根目录调用 `editaplot.cmd`。
> 然后依次跑 `doctor` → `doctor --repair` → `origin-smoke`。
> **不要把仓库精简成 `skill/editaplot` 一个子目录**——绘图引擎在 `runtime/`。

## 安装

要求：**Windows 10/11 x64 实体电脑** + **64 位 CPython 3.10–3.12** + 本机已安装
**Origin / OriginPro 2021–2026b**（2024b / 10.15 是完整验证过的基线）。

```powershell
git clone https://github.com/jz747595-ctrl/orgin_skill.git
cd orgin_skill
.\editaplot.cmd doctor
.\editaplot.cmd doctor --repair
.\editaplot.cmd origin-smoke --output-dir "$env:TEMP\editaplot-smoke"
```

如果你的平台是 Codex，`setup` 默认装到当前用户的 `.codex/skills`；其他平台用 `--target` 指到
你自己的 skill 目录即可。`setup` 只做两件事：把 Skill 放到平台能找到的位置、记住本仓库 `runtime/`
的位置；它本身不参与绘图。**`origin-smoke` 是唯一能证明"这台机器真的能画"的步骤**——它要求本机装好 Origin。

不支持 macOS、Linux、WSL、Wine/CrossOver、Parallels 和其他虚拟机；也不替你安装或修改 Origin。

## 相对上游改了什么

这个副本**只动排版层**，没有改任何数据契约（不重算、不拟合、不插值、不归一化）：

1. **多面板合成**（`editaplot.cmd panel`）——把多张**已经各自验证过**的单图合并成**一个可编辑的多层
   Origin 页**：共享轴网格、可选行列标题、可选整页共享图例。合成器只重放冻结好的绘图计划，
   源文件哈希变了会直接拒绝（`panel_source_changed`），不会静默重算。
2. **误差棒帽宽跟随标记**——Origin 默认把误差棒帽固定画成 8 pt，于是"标记明明缩小了、每个点看起来
   还是根大横杠"。现在按 `1.3 × 标记大小` 下发，并从 `plotN.symbol.size` 回读校验。
3. **每个合成页只保留一个数据工作簿**，而不是每个面板各生成一个，工程保持可导航。

其余改动都在文档：4 个 Skill 文档与安装副本对齐、`docs/installation.md` 增加上面的通用平台路线、
`CHANGELOG.md` 记录了这次本地构建。

## 当前的验证状态（如实说明）

| 项目 | 状态 |
|---|---|
| 仓库自带的发布审计 | 通过：0 错误、0 警告（680 个文件） |
| 从零克隆 → `doctor --repair` | 通过，依赖按锁定版本装齐 |
| `doctor` | `ready_for_render = true`，无阻塞项 |
| `origin-smoke` 实跑出图 | 通过，OPJU / PNG / PDF / TIF 全部生成 |

- **已验证的图形组合是有限的**：多面板只在少数几种图的组合上跑过，不是全模板矩阵。
- 完整测试套件由仓库的 CI 在每次上传时自动运行，结果看仓库的 **Actions** 页。
- 实测环境：Windows 11 x64、Origin 2024b、CPython 3.12.9。

## 你可以指定的样式，优先于模板默认

上传数据时可以直接指定**系列颜色、线宽、填充透明度、画幅比例**，以及**图例显示/无框/位置**——
**明确选择优先**于模板默认。每一项会被标为"已采用 / 保留模板默认 / 拒绝 / 仍需确认"，只有当前模板
已经实现、能在 Origin 里画出来并反读校验的字段才算「已采用」。

## 保留的边界

- **不改你的原始数据**：源 CSV / TXT / XLS / XLSX 只读；派生的辅助列只存在于内存或可编辑的 Origin 工程里。
- **不做未授权的科学计算**：不自动平滑、插值、剔点、拟合，也不重算统计量或加误差棒。
- **列用途先说清楚**：每一列会被归为绘制、辅助、仅计算、保留不画或待确认；**待确认的列会阻止绘图**，
  不会自动猜成一条新曲线。
- 参考图只作为视觉简报，不复制其中的数据、文字或 Logo。

## 隐私与权限

- 本地绘图流程**不会主动把你的数据上传到网络**。但交给 AI 平台的对话内容仍受该平台账号与保留策略约束。
- 医学或人像数据在交给 AI 之前，需要你按所在机构要求自行去标识化；**不承诺自动发现 PHI**。
- 正常流程只需要普通用户权限：**不需要管理员权限**，不需要鼠标控制，也不需要改动 **DCOM**、
  **注册表**、防火墙或 Origin 安装。如果某一步要求你"用管理员运行"，那不是在走正常流程。

## 许可与出处

本仓库基于 [EditaPlot](https://github.com/hang-jin/editaplot)，采用 [Apache License 2.0](LICENSE)。
上游署名与第三方许可保留在 `NOTICE`、`AUTHORS.md`、`THIRD_PARTY_NOTICES.md` 与 `ASSET_PROVENANCE.md` 中。

本项目与 OriginLab 公司**没有**隶属或背书关系；"Origin" 是其各自权利人的商标。
