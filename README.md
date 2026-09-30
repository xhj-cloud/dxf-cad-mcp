# dxf-cad-mcp

轻量级 **DXF CAD MCP 服务器**：让任何 AI agent（Claude / dsh / Cursor …）通过 MCP 协议
画工程图，不依赖 CAD 图形界面。

```
创建 DXF → 绘制实体/标注/填充/图块 → 图层管理 → 检查图纸
        → PNG 预览 / 矢量 PDF 导出 → 一键打开到 CAD
```

- **跨平台**：macOS / Windows / Linux 均可运行（DXF 是 Autodesk 公开规范的交换格式）
- **纯本地**：ezdxf + matplotlib，不需要网络，不需要 CAD 授权即可生成图纸
- **DWG 输出**：`dxf_convert_dwg` 一键 DXF→DWG（LibreDWG，DWG 是专有格式，ezdxf 写不了）
- **macOS + AutoCAD 友好**：内置中文字体（.ttc）与 "白/黑" 双色（ACI 7）渲染修复

## 为什么走 DXF 路线

- AutoCAD for Mac **没有** AppleScript / COM / .NET 接口，直接操控 UI 不可行
  （Windows 上的 CAD MCP 项目大多依赖 COM，难以移植）
- DXF 是公开交换格式：ezdxf 纯本地读写，生成的图纸任何 CAD 软件都能打开
  （AutoCAD / LibreCAD / QCAD / FreeCAD …）
- **"以文件为中心"** 的架构：稳定、可验证、可回放——PNG/PDF 预览与 CAD 终显互相对照

## 特性

- 12 个 MCP 工具，覆盖完整 2D 绘图工作流（见下表）
- 9 种基础实体 + 5 种尺寸标注 + 27 种标准填充图案 + 样条 + 图块
- 颜色三种写法：ACI 数字 / 颜色名（`"red"`）/ `#RRGGBB` 真彩色
- 内置标准线型：Dashed / Center / Hidden / Phantom / Dashdot
- PNG 预览 + 矢量 PDF 导出（auto 自适应或 A4/A3/A2 横版）
- 中文渲染：启动时自动注册系统 CJK 字体，中文标注/文字预览不空白

## 工具列表（11 个）

| 工具 | 说明 |
|------|------|
| `dxf_create` | 创建空 DXF（版本 R12~R2018，默认 R2010） |
| `dxf_draw` | 追加实体：line / polyline / rectangle / circle / arc / ellipse / point / text / mtext；文件不存在自动创建，引用的图层自动创建 |
| `dxf_dimensions` | 追加尺寸标注：linear / aligned / radius / diameter / angular；返回每个标注的实测值 |
| `dxf_hatch` | 追加填充：solid 实底 / ANSI31 等标准图案；闭合多边形边界 |
| `dxf_splines` | 追加样条线（过拟合点集的 2/3 阶样条） |
| `dxf_blocks` | 定义图块（内含 `dxf_draw` 同款实体）并插入引用（旋转/缩放） |
| `dxf_layers` | 创建/更新图层（颜色：ACI 数字 / 颜色名 / #RRGGBB；线型：Continuous/Dashed/Center/Hidden/Phantom/Dashdot） |
| `dxf_read` | 检查图纸：版本、实体统计、类型分布、包围盒、图层 |
| `dxf_render` | 渲染 PNG 预览（ezdxf + matplotlib，不需要打开 CAD） |
| `dxf_export_pdf` | 导出矢量 PDF（线条/文字保持矢量，可 auto 自适应或 A4/A3/A2 横版） |
| `dxf_convert_dwg` | DXF → DWG 转换（LibreDWG dxf2dwg，纯本地；版本 r12/r14/r2000/r2004，默认 r2000） |
| `dxf_open_in_autocad` | 用 macOS `open` 打开到本机 AutoCAD（自动找 /Applications/Autodesk 下的实例，完整版优先） |

## 快速开始

```bash
git clone <repo-url> dxf-cad-mcp && cd dxf-cad-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt     # mcp<2, ezdxf, matplotlib
```

> 可选：`dxf_convert_dwg` 依赖系统安装 **LibreDWG**（提供 `dxf2dwg` 命令）：
> `brew install libredwg`（macOS）/ `apt install libredwg`（Debian/Ubuntu）。
> 未安装时该工具返回安装提示，其余 11 个工具不受影响。

或者直接作为包安装（附带 `dxf-cad-mcp` 命令）：

```bash
pip install .        # 或 pipx install .
```

**不经 MCP 先试一把**（生成一张带标注/填充/图块的示例图）：

```bash
.venv/bin/python examples/quickstart.py
# 输出 examples/quickstart.dxf / .png / .pdf
```

## 接入 MCP 客户端

服务器使用 **stdio** 传输，在任何支持 MCP 的客户端里配置命令行即可。

dsh（`~/.dsh/profiles/web/cordis.patch.yml`）：

```yaml
- insert:
    - id: mcp-dxf-cad
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: dxf-cad
        transport: stdio
        command: /path/to/dxf-cad-mcp/.venv/bin/python
        args: ['/path/to/dxf-cad-mcp/server.py']
        cwd: /path/to/dxf-cad-mcp
        toolCallTimeoutMs: 120000
```

通用 MCP 客户端（Claude Desktop / Cursor 等）：

```json
{
  "mcpServers": {
    "dxf-cad": {
      "command": "/path/to/dxf-cad-mcp/.venv/bin/python",
      "args": ["/path/to/dxf-cad-mcp/server.py"]
    }
  }
}
```

## 测试

```bash
.venv/bin/python test_client.py    # 端到端（第一批）：基础实体 + 渲染
.venv/bin/python test_client2.py   # 端到端（第二批）：标注/填充/样条/图块 + PDF
```

测试产物输出到 `test_output/`（已 gitignore）。

## 已知限制

- 仅 2D：无参数化实体/约束/动态块（DXF 格式本身不含这些数据）
- DXF 写入版本最高 R2018（AC1032），AutoCAD 2018+ 均可读取
- 标注预览使用 ezdxf 简化渲染器，与 CAD 显示可能有差异（AutoCAD 打开时按
  定义点重新渲染，**以 CAD 显示为准**）
- 填充边界仅支持闭合多边形（圆形/弧线边界暂不支持）
- 仅 model space；布局/图纸空间未实现
- `dxf_convert_dwg` 走 LibreDWG 开源实现，成熟度低于 Autodesk 官方：MATERIAL /
  MLEADERSTYLE 等专有对象自动跳过（不影响几何）；转换前会把 MTEXT 旋转角归零
  （绕开 LibreDWG 解析 bug），AutoCAD 打开后标注文字按定义点重渲染，显示不受影响
- `dxf_open_in_autocad` 仅 macOS（依赖 `open` 命令）
- mcp SDK 固定 `<2`：2.x 把 FastMCP 改名 MCPServer，与 v1 客户端生态不兼容

## 项目结构

```
dxf-cad-mcp/
├── server.py            # MCP 服务器（12 个工具）
├── examples/
│   └── quickstart.py    # 直调示例（无需 MCP 协议）
├── test_client.py       # 端到端测试（第一批）
├── test_client2.py      # 端到端测试（第二批）
├── requirements.txt
├── pyproject.toml
├── LICENSE              # MIT
└── README.md
```

## License

[MIT](./LICENSE)
