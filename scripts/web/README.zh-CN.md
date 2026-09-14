# Web 开发工具

本目录用于 Web 本地开发、静态资源维护和手工视觉评估。正常的 Python、Web 和 Desktop 构建既不会执行，
也不会打包这里的脚本。

这些工具生成或检查的运行时文件位于 `src/iac_code/web/static/`。Python 发布包通过 setuptools 收录已提交的
JavaScript、许可证和 NOTICE，Desktop sidecar 则复制同一静态资源目录。因此正常发布只使用仓库里已经提交的
产物，不需要安装 Node.js、pnpm，也不需要准备 Eraser 源码仓库或 ROS 模板语料库。

## Eraser 浏览器 Bundle

`build_eraser_vendor.mjs` 生成仅供浏览器使用的 Eraser bundle。`eraser/browser-entry.js` 是构建入口，负责注册
本地字体和图标，并暴露 sandbox Web frame 使用的解析、渲染适配器。这两个文件都不会被产品运行时直接加载。

构建固定使用 Eraser 提交 `6d377f296b94abf63481a07128884066e4930321`。Eraser 仓库要求 Node 22.12 或更高
版本及 pnpm 10.33。在两个仓库根目录依次执行：

```bash
cd /path/to/eraser-diagrams
corepack pnpm install --frozen-lockfile
corepack pnpm build

cd /path/to/iac-code
node scripts/web/build_eraser_vendor.mjs /path/to/eraser-diagrams
```

该命令更新以下需要提交的发布输入：

- `src/iac_code/web/static/js/vendor/eraser-diagrams.min.js`
- `src/iac_code/web/static/js/vendor/eraser-diagrams.NOTICE`

Eraser 的 MIT 许可证单独维护在 `src/iac_code/web/static/js/vendor/eraser-diagrams.LICENSE`。升级固定版本时应同时
检查这三个文件。源码仓库 HEAD 与固定提交不一致，或者无法收集某个打包依赖的许可证文本时，脚本会直接失败。

这个命令有意不接入正常发布构建。否则每次打包都会额外依赖外部 Git checkout 和 Node 工具链。升级 Eraser 时应
显式重新生成并提交 vendor 文件。

## 固定语料布局评估

`evaluate_eraser_corpus.py` 通过公开预览 API 和浏览器使用的同一个布局 Worker 检查固定的 30 个 ROS 模板，输出
同级节点重叠、容器包含失败、画布越界、连线穿过节点和极端长宽比等指标。它是确定性几何检查，不调用 LLM。

先启动本地 Web 服务，再运行：

```bash
uv run iac-code web --host 127.0.0.1 --port 8766

uv run python scripts/web/evaluate_eraser_corpus.py \
  --templates-root /path/to/ros-templates \
  --base-url http://127.0.0.1:8766 \
  --output /tmp/iac-code-diagram-eval/run
```

语料清单固定了预期的 `ros-templates` 提交。只有明确需要比较其他版本时才使用
`--allow-template-revision`。评估器会读取模板并把内容 POST 到 `--base-url`；除非明确需要发送给其他服务，否则应
保留默认的本机回环地址。JSON 和 Markdown 报告写入指定输出目录，不进入发布包。

评估脚本没有接入 `make test`，因为它依赖单独的模板仓库、Node.js 和正在运行的 Web 服务。预览 API、图投影、
布局 Worker、静态资源与打包规则的单元及集成测试仍位于 `tests/`。
