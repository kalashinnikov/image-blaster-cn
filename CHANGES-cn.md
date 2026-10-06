# 本分支与原版的差异

- 上游仓库：<https://github.com/neilsonnn/image-blaster>
- 基线：上游 `main` 的 `4acb43b`
- 对照方式：上游 `main` 共 140 个文件，本仓库与其逐文件比对——**124 个文件完全一致**，15 个 `.mjs` 被修改，另有若干新增文件（不含运行时目录）。
- 本文只列**差异**，不重复上游文档。上游功能说明见 [`README.upstream.md`](README.upstream.md)。

## 一、新增

### 1. `.claude/scripts/asset-pipeline/hunyuan-3d-tencent.mjs`

腾讯云混元生3D provider，替代 fal.ai 的 Hunyuan 3D：

- 自签 **TC3-HMAC-SHA256**（用 Node 内置 `crypto` + `fetch`，**零第三方依赖**）
- 支持两条线：Pro（`SubmitHunyuanTo3DProJob` / `QueryHunyuanTo3DProJob`）与极速版（`SubmitHunyuanTo3DRapidJob` / `QueryHunyuanTo3DRapidJob`）
- 固定 `Region=ap-guangzhou`、`Version=2025-05-13`（腾讯云该接口只有华南广州地域）
- `--check`：零成本凭据探针；`--dry-run`：打印已签名的请求（密钥打码）
- 默认 `ResultFormat=GLB`：因为返回的 OBJ 是 **zip 包**，直接下会拿到压缩包
- 默认 `FaceCount=50000` + `EnablePBR=true` ≈ 40 积分 ≈ ¥4/次；`--face-count 0` 可省略该参数，用服务端默认 500000

### 2. `dsh-port/SKILL.md`

DeepSeek Harness 技能文档：在 DSH 里驱动同一套流水线（建项目 → 分析图片 → 生成物体 3D → 查看器）。把上游的 Claude Code 技能映射成 DSH 的表述，并记录了 Windows 上的注意事项。

### 3. `tools/retexture/`

把照片里的真实细节（尤其是**牌子上的字**）按几何投影刷回模型贴图的工具链，用来解决「单图生 3D 的贴图是模型猜的、字必然糊」的问题。详见 [`tools/retexture/README.md`](tools/retexture/README.md)。

### 4. `pnpm-workspace.yaml` / `pnpm-lock.yaml`

改用 pnpm 的 workspace 配置与锁文件（上游用 bun）。

### 5. `README.upstream.md` / 本文件

上游英文 README 原文备份；以及你正在看的差异说明。

## 二、修改的上游文件（15 个）

### 2.1 Windows 主入口守卫（15 个文件共有）

原写法在 Windows 上恒为 false，导致 `node script.mjs` **静默退出、什么都不做**：

```js
if (import.meta.url === `file://${process.argv[1]}`) {
```

改为：

```js
import { pathToFileURL } from "node:url";
...
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
```

涉及文件：

- `.claude/scripts/asset-pipeline/`：`generate-single-asset.mjs`、`gpt-image-2-edit.mjs`、`hunyuan-3d.mjs`、`image-edit.mjs`、`meshy-3d.mjs`、`nano-banana-edit.mjs`
- `.claude/scripts/fal/run-fal.mjs`
- `.claude/scripts/image-edit/generate-edit.mjs`
- `.claude/scripts/project/`：`delete.mjs`、`download.mjs`、`ensure-local-assets.mjs`、`indexed-path.mjs`、`project-state.mjs`
- `.claude/scripts/sfx/fal-elevenlabs-sfx.mjs`
- `.claude/scripts/world/generate-world.mjs`

### 2.2 `generate-single-asset.mjs` 额外改动：接入腾讯云 provider

- `MODEL_PROVIDER_ALIASES` 增加 7 个别名：`tencent`、`tencent-hunyuan`、`hunyuan-tencent`、`hunyuan3d-tencent`、`tencent-ai3d`、`tencent-ai3d/hunyuan-to-3d-pro`
- `run3DProvider()` 增加分派；`resolve3DProvider()` 的报错文案改为 `Use one of: meshy, hunyuan, hunyuan-tencent.`
- 新增 `--rapid` 开关（走极速版，约 15 积分）
- usage 串更新为 `--provider hunyuan|meshy|hunyuan-tencent`

### 2.3 `README.md`

重写为中文，并在顶部显著标注上游来源、基线 commit、MIT 许可与原作者（上游英文原文另存 `README.upstream.md`）。

### 2.4 `.gitignore`

在保留上游规则的基础上补充：Python 缓存、`tools/retexture/out/`、`*.log`。

## 三、未改动

上游其余 **124 个文件**原样保留，包括：

- `app/`（查看器全部代码：React、react-three-fiber、SparkJS、Rapier）
- `.claude/skills/`（8 个 Claude Code 技能包）
- `.claude/agents/`（6 个子代理定义）
- `.claude/rules/project.md`（上游工作流规范）
- `LICENSE.md`、`package.json`、`.cursor/rules/` 等

## 四、不包含

`.env`（含密钥）、`worlds/`、`input/`（用户数据与生成产物）、`node_modules/`、`app/dist/`。

## 五、如何与上游同步

```bash
git remote add upstream https://github.com/neilsonnn/image-blaster
git fetch upstream
git merge upstream/main       # 或 git rebase upstream/main
```

上游改动集中在 `.claude/scripts/` 时，注意保留 §2.1 的 Windows 守卫修复。
