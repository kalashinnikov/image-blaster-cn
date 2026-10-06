<img width="960" height="540" alt="image-blaster-1" src="https://github.com/user-attachments/assets/d294e420-eb48-4f00-b6a8-13005442d1a8" />

# image-blaster-cn

## 站在巨人的肩膀上

**本仓库是 [neilsonnn/image-blaster](https://github.com/neilsonnn/image-blaster) 的中文化分支（fork）。**

创意、工作流设计、Claude 技能包、3D 查看器，全部出自原作者 **Neilson Koerner-Safrata**。本分支只是把它搬到国内的模型服务上、修掉 Windows 上的一个致命 bug，并补了一套用真实照片修正贴图的工具。

| | |
| --- | --- |
| 上游仓库 | https://github.com/neilsonnn/image-blaster |
| 本分支基线 | 上游 `main` 的 `4acb43b` |
| 作者 | Neilson Koerner-Safrata（上游全部原创工作） |
| 许可证 | MIT，原始版权见 [`LICENSE.md`](LICENSE.md)（**未做任何修改**） |
| 上游英文说明 | [`README.upstream.md`](README.upstream.md)（原文保留） |
| 本分支的改动 | [`CHANGES-cn.md`](CHANGES-cn.md)（逐条可核对） |

> 只想要原版？请直接去上游仓库。本分支的存在意义是：**在中国大陆的网络与付费条件下，也能把这条路走通**。

## 这个分支做了什么

一句话：**物体 3D 从 fal.ai 换成腾讯云混元生3D**（国内直连、人民币结算、有免费额度），修掉原版在 Windows 上「静默什么都不做」的 bug，并额外给了一套把照片里真实文字投影回模型贴图的工具。

| 环节 | 原版 | 本分支 |
| --- | --- | --- |
| 物体 3D | fal.ai 上的 Hunyuan 3D（$0.375/次，需外币卡） | **腾讯云混元生3D**（约 40 积分 ≈ ¥4/次，Pro 与极速版都可选） |
| 探索式 3D 世界 | World Labs Marble（1,500 credits/次） | **未改**，仍是境外服务（可选步骤） |
| 音效 | fal.ai 上的 ElevenLabs SFX | **未改**，仍是境外服务（可选步骤） |
| 参考图清理 | fal.ai 的 nano-banana-2 edit | 未改；也可以直接用原图当参考图，省掉这一步 |
| Windows 支持 | **脚本静默退出、什么都不做** | 已修复（根因见下） |
| 依赖管理 | bun | pnpm |
| 贴图上的字 | 模型猜，必然糊 | 可选：`tools/retexture` 用照片真实像素覆盖 |
| 驱动方式 | Claude Code 技能 | Claude Code 技能（原样保留）+ DeepSeek Harness 技能（`dsh-port/`） |

## 快速开始（Windows）

```powershell
git clone https://github.com/kalashinnikov/image-blaster-cn
cd image-blaster-cn

# 1. 装上依赖（pnpm；Node 20+）
pnpm install

# 2. Windows 必须：public 下的 worlds 需要是真正的目录联接，不能是那个链接文件
Remove-Item app\public\worlds -Force -ErrorAction SilentlyContinue
cmd /c mklink /J app\public\worlds worlds

# 3. 配置密钥
Copy-Item .env.example .env
#   编辑 .env，至少填 TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY

# 4. 起查看器
pnpm --dir app run dev     # 或直接 node app/node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5173
```

腾讯云密钥在 <https://console.cloud.tencent.com/cam/capi> 创建；混元生3D 在 <https://console.cloud.tencent.com/ai3d> 开通，并到 <https://console.cloud.tencent.com/ai3d/packages> 领新用户免费额度。

### 先验一下密钥是否通（不花钱）

```powershell
node .claude/scripts/asset-pipeline/hunyuan-3d-tencent.mjs --check
```

签名/区域/接口都对的话，会返回一个「任务不存在」之类的业务错误，而不是 `AuthFailure`。

### 跑一遍最小闭环

```powershell
# 建项目 + 把图片放进 input/
node .claude/scripts/project/project-state.mjs --world my-world --stage-input

# 生成一个物体的 3D（Pro 版；--rapid 走极速版，更便宜更快）
node .claude/scripts/asset-pipeline/generate-single-asset.mjs `
  --world my-world --object-id my-object --provider tencent --rapid
```

## 费用

| 服务 | 价格 | 说明 |
| --- | --- | --- |
| 腾讯云混元生3D（极速版） | 约 15 积分 ≈ ¥1.5/次 | 形状快检够用，贴图质量一般 |
| 腾讯云混元生3D（Pro） | **约 40 积分 ≈ ¥4/次** | 默认 `Normal + PBR + 50000 面`；新用户有 100 积分免费额度 |
| World Labs Marble（可选） | 1,500 credits/次 | 生成可探索的 3D 世界，境外服务 |
| fal.ai（可选） | Hunyuan 3D $0.375/次、改图 $0.08/张、音效 $0.002/秒 | 音效与参考图清理仍走这里；境外服务 |

积分制按腾讯云文档：**0.1 元/积分**，用不完的额度不会自动扣费（未开启后付费时服务自动停止）。

## 目录结构

```
.claude/scripts/            生成流水线（Node，跨平台；已修 Windows 主入口守卫）
.claude/skills/             上游的 Claude Code 技能包（原样保留）
app/                        3D 查看器（React + react-three-fiber，原样保留）
dsh-port/SKILL.md           DeepSeek Harness 技能：在 DSH 里驱动同一套流程
tools/retexture/            重投影工具：把照片里的真实文字/纹理刷回模型贴图
worlds/  input/             运行时目录（不进 git）
```

## 这个分支修了什么

**Windows 主入口守卫 bug。** 上游 15 个脚本用这样的写法判断自己是不是主模块：

```js
if (import.meta.url === `file://${process.argv[1]}`) { ... }
```

在 Windows 上 `import.meta.url` 是 `file:///E:/...`（三斜杠、正斜杠、中文路径还会被百分号转义），而 `process.argv[1]` 是 `E:\...`，比较**恒为 false** —— 于是 `node xxx.mjs` 会**静默退出、什么都不做**，连报错都没有。改成：

```js
if (import.meta.url === pathToFileURL(process.argv[1]).href) { ... }
```

完整清单与其它改动见 [`CHANGES-cn.md`](CHANGES-cn.md)。

## 贴图上的字怎么办

单图生 3D 的贴图是模型「猜」出来的，字一定是糊的。本分支带了一套工具，把照片里的真实像素按几何投影刷回贴图：

```powershell
python tools/retexture/retexture.py retexture model.glb photo.jpg out.glb out/ front `
  --photo-quad "455,282 746,282 761,1225 468,1225"
```

用法、原理与两个必须知道的坑见 [`tools/retexture/README.md`](tools/retexture/README.md)。

## 上游文档

上游的功能说明（世界生成、音效、Hunyuan 参数、查看器开发等）原文保留在 [`README.upstream.md`](README.upstream.md)。

## 许可证

MIT。原始版权归 Neilson Koerner-Safrata，见 [`LICENSE.md`](LICENSE.md)。本分支的修改同样以 MIT 发布。
