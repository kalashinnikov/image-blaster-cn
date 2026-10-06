---
name: image-blast
description: Use when the user mentions IMAGE-BLAST, image-blaster, "blast this image", worlds/<slug>, a photo sitting in input/, or asks to turn a source image into a 3D environment, per-object 3D models, or sound effects; also use for the ported DSH setup here — .env keys (WORLD_LABS_API_KEY, FAL_KEY), bundled node/pnpm instead of bun, missing-node or exit-0-with-no-output script failures, and the asset viewer on port 5173.
---

# IMAGE-BLAST (DSH port)

This is the DeepSeek Harness port of the Claude Code pack. The **engine is unchanged**:
all real work happens in `.claude/scripts/**` (plain Node ESM). This skill only replaces
Claude Code's scheduling/command layer.

Repo root: this skill is `<repo>/dsh-port/SKILL.md`, so the repo root is its grandparent
directory. Run every script from there (they use repo-relative paths).

## Runtime rules (read before running anything)

1. **`node` is not on PATH.** Get it once per session and reuse:

   ```powershell
   $Node = (load_workspace_dependencies).node   # -> ...\dsh-runtimes\...\node\bin\node.exe
   ```

   From the shell: `$env:DSH_HOME\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe`.
   `bun` is NOT installed — use pnpm (`...\dependencies\pnpm\bin\pnpm.mjs`, also via node) for the viewer.
   pnpm 11 blocks `esbuild`'s build script; run vite directly (see step 4) instead of `pnpm run dev`.
2. **Always run scripts from the repo root.** They use repo-relative paths (`worlds/...`, `input/`).
3. **Generation scripts block until the API call finishes.** Run them in the *foreground*
   (normal tool call, no `run_in_background`) and read stdout. Never tail them, never start
   them as a background job.
4. **Never read generated PNG/JPG/WEBP into context for QC.** Trust stdout + indexed filenames.
   Only load a source image when visual analysis is the actual task.
5. **Windows fix already applied.** All 15 CLI scripts had the naive guard
   `import.meta.url === \`file://${process.argv[1]}\`` which is always false on Windows
   (backslash + drive letter) — every script silently exited 0 without doing anything.
   Patched to `process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href`
   with `import { pathToFileURL } from "node:url";` added. Do not "simplify" it back.

## Setup

- `.env` at repo root holds `WORLD_LABS_API_KEY` (worlds) and `FAL_KEY` (3D / SFX / image edit).
  It is seeded from `.env.example` with placeholder values. **Never ask the user to paste a key**;
  tell them to edit `.env` themselves. `scripts/project/project-state.mjs` and the no-cost
  analysis steps work without keys.
- Tencent Cloud Hunyuan 3D (the domestic alternative that needs no FAL_KEY) reads
  `TENCENTCLOUD_SECRET_ID` / `TENCENTCLOUD_SECRET_KEY` (plus optional `TENCENTCLOUD_TOKEN`) from the
  same `.env`. Always verify them with the **zero-cost** probe before spending anything:
  `& $Node .claude\scripts\asset-pipeline\hunyuan-3d-tencent.mjs --check`
  → `{"ok":true}` means the TC3 signature and the key pair are good;
  `AuthFailure.SecretIdNotFound` means the values are wrong or the service was never activated.
  `--dry-run` prints the signed request (keys redacted) and spends nothing.
- `app/public/worlds` is a Windows directory junction to `..\..\worlds` (the zip shipped a
  unix symlink stub). Recreate with `cmd /c mklink /J app\public\worlds worlds` if it is lost.

## Loop

### 1. Project envelope (no API cost)

```powershell
& $Node .claude\scripts\project\project-state.mjs --world "<slug>" --stage-input
```

`--stage-input` moves images from `input/` into `worlds/<slug>/source/` as `0-<slug>.<ext>`.
Prints full project state JSON. Create `worlds/<slug>/` subdirs `source/`, `output/`,
`output/world/`, `output/sfx/` plus `project.json` — this is what it does.

### 2. Uncover / analyse (no API cost)

Follow `.claude\skills\image-blast-uncover\IMAGE-BLAST.md` **exactly** for the JSON contract
(flat schema, no `images[]`, literal observational language, atomic single-item objects only,
never compound assets, skip environment surfaces). Read the source image directly, then write:

- `worlds/<slug>/source/<image-name>.json` — per-image analysis, same flat schema, no `images[]`
- `worlds/<slug>/image.json` — merge of all `source/*.json` (combine `source_images`, synthesise
  shared `scene_name`/`short_caption`/`literal_description`, merge environment/lighting/etc,
  dedupe objects by id, preserve evidence)
- `worlds/<slug>/output/<object-slug>/object.json` — only after the user confirms objects;
  durable identity/provenance only (no status/jobs/files/request lifecycle)

Present the analysis, then **stop and wait** for the user to confirm which objects to make.

`object.json` shape gotcha (verified against `generate-single-asset.mjs:225`): the file must be
**wrapped** in an `object` key, because `resolveObject()` only accepts `existing?.object` and
otherwise throws `Object file not found: worlds/<slug>/output/<id>/object.json`:

```json
{ "schema_version": 1, "world": "<slug>", "object": { "id": "...", "name": "...", "description": "...",
  "materials": ["..."], "source_images": ["worlds/<slug>/source/0-<slug>.jpg"],
  "evidence": [{ "image": "worlds/<slug>/source/0-<slug>.jpg", "location_in_image": "..." }],
  "generate_as_3d_object": true, "working_dir": "worlds/<slug>/output/<id>" },
  "updated_at": "<iso>" }
```

`source_images` + every `evidence[].image` are fed to the nano-banana edit as inputs, so they must
be repo-relative paths that exist. Optional per-object overrides: `model_provider`, `image_edit_provider`.
`status` is stripped by the script, so do not store job state in this file.

### 3. One 3D object (costs FAL_KEY or Tencent credits)

```powershell
& $Node .claude\scripts\asset-pipeline\generate-single-asset.mjs `
  --world "<slug>" --object-id "<object-slug>" `
  --image-edit-prompt "Isolate the <target object> from this image. Reproduce it exactly as shown -- same colors, materials, and proportions. White background, centered, tight crop, studio lighting. No other objects, no scene, no people, no text, no shadows on the ground."
```

Defaults: Hunyuan (`--provider hunyuan`), `--face-count 50000`, `--enable-pbr true`,
`--generate-type Normal`. Also accepts `--image <path> --object-name "<n>" --description "<d>"`
for direct single-image generation, `--reference-only` to stop after the reference PNG,
`--regenerate` / `--regenerate-reference` for new runs, `--provider meshy` for Meshy.
`--rapid` (Tencent only) switches to the cheap Express tier for a silhouette check before a Pro run.
Prints `{object, output_dir, reference_image, model_files, request_metadata}`.

#### 3b. Tencent Cloud Hunyuan 3D (`--provider tencent`) — no FAL_KEY needed

`.claude/scripts/asset-pipeline/hunyuan-3d-tencent.mjs` implements `runHunyuan3DTencent()` with the
same calling contract as `runHunyuan3D()`, so the dispatcher only needs `--provider tencent`
(aliases: `tencent`, `tencent-hunyuan`, `hunyuan-tencent`, `hunyuan3d-tencent`, `tencent-ai3d`).
It signs the request itself with TC3-HMAC-SHA256 (node `crypto`, no SDK, no new dependency),
submits `SubmitHunyuanTo3DProJob`, polls `QueryHunyuanTo3DProJob` (JobId is valid 24 h) and saves
the GLB as `<index>-hunyuan-tencent-<asset>.glb` for `normalizeModelFiles()` to rename.

```powershell
& $Node .claude\scripts\asset-pipeline\generate-single-asset.mjs `
  --world "<slug>" --object-id "<object-slug>" --provider tencent `
  --face-count 50000 --enable-pbr true
```

- Only region `ap-guangzhou` is served; `--result-format GLB` (default) avoids the OBJ `.zip`
  the Pro job otherwise returns. `--model 3.0` (default) or `3.1` (3.1 rejects LowPoly).
- If `output/<id>/0-<id>.<ext>` already exists it is reused as the reference image, which skips the
  paid nano-banana edit — dropping a cropped/cleaned photo there makes a 3D run possible with **no
  FAL_KEY at all**.
- In-flight metadata deliberately stores `job_id` + `status: "running"` and **no** `request_id`/
  `endpoint`: those two fields would make the dispatcher treat the record as a resumable *fal*
  request and poll `queue.fal.run`. The finished summary carries `job_id` + `request_id` (Tencent
  JobId), `status: "completed"`, `credit_consumed` and `credit_details`.
- **Cheap shape check first** — add `--rapid` (`SubmitHunyuanTo3DRapidJob` / `QueryHunyuanTo3DRapidJob`,
  a fraction of the Pro price, ~78 s in practice). The Express tier ignores `--model`,
  `--generate-type` and `--face-count` and accepts only `ResultFormat` / `EnablePBR` /
  `--enable-geometry`, so use it to validate the reference image's silhouette; its metadata is
  tagged `tier: "rapid"` and `provider_slug: hunyuan-tencent-rapid`, and its response carries no
  `credit_consumed` — read the real cost off the console.
- **Reference image shapes the mesh.** A tightly cropped 1:4 slab with no background makes the
  service guess a fat box; pad the object onto a near-square canvas with a clean background and a
  little air around it, and the same photo yields a proper column. Also: any leftover clutter at
  the crop's edge (a person, railings) gets modelled as geometry — clean it before spending credits.

Repeat per confirmed object. World (`generate-world.mjs`, needs `WORLD_LABS_API_KEY`),
clean plate (`image-blast-plate` flow), SFX (`fal-elevenlabs-sfx.mjs`) and generic image edit
(`generate-edit.mjs`) are **out of scope of this ported loop** — invoke those scripts directly
per the matching `.claude/skills/*/SKILL.md` if asked.

### 4. Viewer

Deps are already installed (`node_modules` at the repo root, `app/node_modules`). First time
only, or after changing deps:

```powershell
Set-Location <repo root>; & $Node $pnpm install --reporter=append-only
```

Start the server as a **managed background job** (it must keep running), then verify the URL:

```powershell
Set-Location <repo root>\app
& $Node 'node_modules\vite\bin\vite.js' --host 127.0.0.1 --port 5173 --strictPort
```

Do **not** use `pnpm --dir app run dev`: pnpm runs a deps check before the script, and
`esbuild@0.21.5`'s build script is blocked by pnpm 11 (`ERR_PNPM_IGNORED_BUILDS`, exit 1) —
pnpm's pre-run check turns that warning into a hard failure. esbuild itself works fine (its
platform binary comes from the installed `@esbuild/win32-x64` package, verified), so the direct
vite invocation is the supported path. If you prefer `pnpm run dev`, approve the script first
with `pnpm approve-builds` (interactive, writes `onlyBuiltDependencies`).

Verified on this machine: `GET http://127.0.0.1:5173/` → 200, `<title>image-blaster</title>`;
`GET /worlds/junction-probe.txt` → 200 `junction-ok`, which proves the
`app/public/worlds` → `worlds/` junction is served. Report <http://127.0.0.1:5173> and tell the
user to open it.

## Mapping: Claude Code -> DSH

| Claude Code construct | Here |
| --- | --- |
| `.claude/skills/image-blast-*` (8 skills) | this skill + the original `.claude/skills/*/SKILL.md` as reference specs |
| `Agent(image-blast-x)` / `run_in_background: true` | `subagent` (background by default) running the matching section of the reference spec; `subagent_fork` when it needs this conversation |
| `$0` / `$ARGUMENTS` (`/image-blast-3d kw ...`) | plain natural-language arguments |
| `.claude/rules/project.md` | this file (conventions inlined) |
| `.claude/hooks/{setup-check,input-check}.sh` | DSH has no hook API — check `.env` and `input/` manually at the start of a run |
| `.claude/agents/*.md` | not ported; their content is already in the corresponding `SKILL.md` |
| `bun install && bun run dev` | `pnpm install` + direct `node app\node_modules\vite\bin\vite.js` (pnpm 11 blocks esbuild's build script) |
| `lsof -i :5173 -sTCP:LISTEN -n -P` (unix) | `Get-NetTCPConnection -LocalPort 5173 -State Listen` |

## Cost (per published pricing)

FAL: Hunyuan 3D `$0.375` + `$0.15` PBR + `$0.15` custom face-count ≈ **$0.675 per object**
(repo defaults); nano-banana-2 edit `$0.08`/image; ElevenLabs SFX v2 `$0.002`/s.
World Labs Marble 1.1: **1,500 credits per world generation**.
A five-object blast ≈ **$4 + 1,500 credits**. Confirm with the user before a full run.

Tencent Cloud Hunyuan 3D (`ai3d`): credits at ¥0.1 each. The Pro job with this repo's defaults
(Normal + PBR + FaceCount 50000) bills roughly **40 credits ≈ ¥4 per object** — the official query
sample reports `ResultCreditDetails {"GenerateType-Normal":20,"Pbr":10,"FaceCount":10}`. Passing
`--face-count 0` omits FaceCount (server default 500000 faces, heavier mesh) and `--enable-pbr false`
drops another line item. A newly activated account gets 100 free credits (≈2 runs at defaults).
Always read `credit_consumed` from the request metadata instead of trusting estimates.
