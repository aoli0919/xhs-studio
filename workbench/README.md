# 本地工作台

工作台用 Python 标准库提供本地桥接，网页负责草稿编辑与预览，小红书登录和提交由上游 `xiaohongshu-mcp` 后端执行。无需 npm install、Node.js 或 Go 编译；Node.js 仅用于开发时的 JavaScript 语法检查。

## 启动

在仓库根目录运行：

```bash
python3 workbench/run.py
```

Python 版本要求为 3.11+。首次运行从 [上游 Releases](https://github.com/xpzouying/xiaohongshu-mcp/releases/tag/v2.5.5) 下载固定 v2.5.5 文件，校验 SHA256。启动器管理的后端默认在 `127.0.0.1:18061`，工作台在 <http://127.0.0.1:18088>。首次运行后端还需下载浏览器，需保持网络可用。

```bash
# 只准备依赖
python3 workbench/run.py --install-only

# 自定义端口，关闭自动打开浏览器
python3 workbench/run.py --ui-port 18088 --backend-port 18061 --no-open

# 使用已有本地后端；令牌文件使用绝对路径
python3 workbench/run.py --connect-existing http://127.0.0.1:18061 --backend-token-file /absolute/path/to/token
```

## 图文流程

创建草稿 → 编辑标题、正文、标签 → 上传图片 → 扫码登录并核对账号 → 预览与校验 → 手动确认提交。

编辑和保存草稿不会发布。图片通过本地工作台上传到本机运行目录；提交时由后端上传到小红书。二维码登录需要用户用小红书 App 完成扫码。当前工作台暂无视频编辑和发布界面，上游 MCP 的视频功能仍可单独使用。

每次预览产生一次性确认凭据，提交使用预览时的内容快照。已提交或结果未知的相同快照会持久锁定。结果未知时请先到小红书核对，确认没有发布后，再点击“已核对，未发布，解除锁定”；解除锁定本身不会发布，需要重新预览和确认。

## 静态演示

`web/` 可静态托管，用于演示草稿、编辑和预览。静态演示没有本地 Python 桥接，无法登录或真实发布；页面演示不能替代完整流程验证。

## 本机数据

运行数据保存在 `workbench/.runtime/`，包括 token、cookies、uploads 和 drafts。此目录不应进入 Git 或源码交付包。草稿和媒体属于用户本机数据；重新下载代码时不要覆盖已有运行数据。

提交记录位于同一目录的 `receipts/`。不要通过删除记录来跳过结果核对。自定义 `--runtime-dir` 必须放在仓库外；仓库内部只允许使用默认、已忽略的 `.runtime/` 路径。

## 验证

```bash
python3 -m unittest discover -s workbench/tests
node --check workbench/web/app.js
```

单元测试与语法检查不执行真实发帖。目前真实发帖尚未测试，等待用户扫码和具体发布内容。提交后应到小红书核对笔记及审核结果。
