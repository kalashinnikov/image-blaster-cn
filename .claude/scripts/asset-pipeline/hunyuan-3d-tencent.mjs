#!/usr/bin/env node
/**
 * Tencent Cloud Hunyuan 3D (腾讯云混元生3D) provider for image-blaster.
 *
 * Drop-in alternative to the fal.ai Hunyuan provider: same calling contract as
 * runHunyuan3D() (see ./hunyuan-3d.mjs), but the job is submitted to Tencent Cloud
 * ai3d.tencentcloudapi.com with a TC3-HMAC-SHA256 signature instead of the fal queue.
 *
 * Credentials come from .env (run from the repo root):
 *   TENCENTCLOUD_SECRET_ID=AKID...
 *   TENCENTCLOUD_SECRET_KEY=...
 *   TENCENTCLOUD_TOKEN=...        # optional, temporary credentials only
 * Aliases also accepted: TENCENT_SECRET_ID / TENCENT_SECRET_KEY.
 *
 * Cost note: the Pro job bills per job. Passing FaceCount (anything other than the
 * server default 500000) and EnablePBR each add separate credit line items; the
 * response's ResultCreditDetails / ResultCreditConsumed are recorded in the request
 * metadata so every run's real cost is auditable.
 *
 * Tiers: the default Pro tier (SubmitHunyuanTo3DProJob) accepts Model / GenerateType /
 * FaceCount / PolygonType. `rapid: true` (CLI `--rapid`, pipeline `--rapid`) switches to
 * the Express tier (SubmitHunyuanTo3DRapidJob), which is much cheaper but accepts only
 * ImageBase64 / ResultFormat / EnablePBR / EnableGeometry — no FaceCount, no GenerateType,
 * no Model — so it is the right tool for a cheap shape check before a Pro run.
 */
import { createHash, createHmac } from "node:crypto";
import { readFile, stat } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

import {
  DEFAULT_HUNYUAN_ENABLE_PBR,
  DEFAULT_HUNYUAN_FACE_COUNT,
  DEFAULT_HUNYUAN_GENERATE_TYPE,
  DEFAULT_HUNYUAN_POLYGON_TYPE
} from "./hunyuan-3d.mjs";
import {
  downloadFile,
  ensureDir,
  loadDotEnv,
  one,
  parseArgs,
  pathExists,
  safeFileName,
  sanitizeForMetadata,
  slugify,
  writeJson
} from "./fal-queue.mjs";
import { buildRequestSummary, requestPath } from "./request-metadata.mjs";

export const HUNYUAN_TENCENT_3D_PROVIDER = "hunyuan-tencent";
export const TENCENT_AI3D_ENDPOINT = "tencent-ai3d/hunyuan-to-3d-pro";
export const TENCENT_AI3D_HOST = "ai3d.tencentcloudapi.com";
export const TENCENT_AI3D_SERVICE = "ai3d";
export const TENCENT_AI3D_VERSION = "2025-05-13";
export const TENCENT_AI3D_REGION = "ap-guangzhou";
export const TENCENT_SUBMIT_ACTION = "SubmitHunyuanTo3DProJob";
export const TENCENT_QUERY_ACTION = "QueryHunyuanTo3DProJob";
export const TENCENT_RAPID_SUBMIT_ACTION = "SubmitHunyuanTo3DRapidJob";
export const TENCENT_RAPID_QUERY_ACTION = "QueryHunyuanTo3DRapidJob";
export const TENCENT_AI3D_RAPID_ENDPOINT = "tencent-ai3d/hunyuan-to-3d-rapid";
export const DEFAULT_TENCENT_RAPID_PROVIDER_SLUG = "hunyuan-tencent-rapid";
export const TENCENT_RAPID_RESULT_FORMATS = new Set(["OBJ", "GLB", "STL", "USDZ", "FBX", "MP4"]);
export const TENCENT_MAX_IMAGE_BYTES = 6 * 1024 * 1024;
export const TENCENT_SERVER_FACE_COUNT = 500000;
export const DEFAULT_TENCENT_3D_MODEL = "3.0";
export const DEFAULT_TENCENT_RESULT_FORMAT = "GLB";
export const DEFAULT_TENCENT_POLL_INTERVAL_MS = 5000;
export const DEFAULT_TENCENT_TIMEOUT_MINUTES = 20;
export const DEFAULT_TENCENT_PROVIDER_SLUG = "hunyuan-tencent";

const GENERATE_TYPES = new Map([
  ["normal", "Normal"],
  ["lowpoly", "LowPoly"],
  ["geometry", "Geometry"],
  ["sketch", "Sketch"]
]);
const POLYGON_TYPES = new Map([
  ["triangle", "triangle"],
  ["quadrilateral", "quadrilateral"]
]);
const TENCENT_MODELS = new Set(["3.0", "3.1"]);
const TENCENT_RESULT_FORMATS = new Set(["OBJ", "GLB", "STL", "USDZ", "FBX"]);
const TENCENT_IMAGE_EXTENSIONS = new Set([".jpg", ".jpeg", ".png", ".webp"]);

function sha256Hex(value) {
  return createHash("sha256").update(value).digest("hex");
}

function hmac(key, value) {
  return createHmac("sha256", key).update(value).digest();
}

function hmacHex(key, value) {
  return createHmac("sha256", key).update(value).digest("hex");
}

function parseBoolean(value, fallback = false) {
  if (value === undefined || value === null) return fallback;
  if (typeof value === "boolean") return value;
  const normalized = String(value).trim().toLowerCase();
  if (["", "true", "1", "yes", "on"].includes(normalized)) return true;
  if (["false", "0", "no", "off"].includes(normalized)) return false;
  return fallback;
}

function parseNumber(value, fallback) {
  if (value === undefined || value === null || value === "") return fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function normalizeGenerateType(value = DEFAULT_HUNYUAN_GENERATE_TYPE) {
  const key = String(value || DEFAULT_HUNYUAN_GENERATE_TYPE).trim().toLowerCase();
  const resolved = GENERATE_TYPES.get(key.replace(/[^a-z]/g, ""));
  if (!resolved) {
    throw new Error(`Unsupported generate type "${value}". Use one of: ${[...GENERATE_TYPES.values()].join(", ")}.`);
  }
  return resolved;
}

function normalizePolygonType(value = DEFAULT_HUNYUAN_POLYGON_TYPE) {
  const key = String(value || DEFAULT_HUNYUAN_POLYGON_TYPE).trim().toLowerCase();
  const resolved = POLYGON_TYPES.get(key);
  if (!resolved) {
    throw new Error(`Unsupported polygon type "${value}". Use one of: ${[...POLYGON_TYPES.values()].join(", ")}.`);
  }
  return resolved;
}

function normalizeModel(value = DEFAULT_TENCENT_3D_MODEL) {
  const normalized = String(value || DEFAULT_TENCENT_3D_MODEL).trim();
  if (!TENCENT_MODELS.has(normalized)) {
    throw new Error(`Unsupported Tencent model "${value}". Use one of: ${[...TENCENT_MODELS].join(", ")}.`);
  }
  return normalized;
}

function normalizeResultFormat(value = DEFAULT_TENCENT_RESULT_FORMAT, formats = TENCENT_RESULT_FORMATS) {
  const normalized = String(value || DEFAULT_TENCENT_RESULT_FORMAT).trim().toUpperCase();
  if (!formats.has(normalized)) {
    throw new Error(`Unsupported result format "${value}". Use one of: ${[...formats].join(", ")}.`);
  }
  return normalized;
}

export async function resolveTencentCredentials() {
  await loadDotEnv();
  const secretId =
    process.env.TENCENTCLOUD_SECRET_ID || process.env.TENCENT_SECRET_ID || process.env.TENCENT_AI3D_SECRET_ID;
  const secretKey =
    process.env.TENCENTCLOUD_SECRET_KEY || process.env.TENCENT_SECRET_KEY || process.env.TENCENT_AI3D_SECRET_KEY;
  const token = process.env.TENCENTCLOUD_TOKEN || process.env.TENCENT_TOKEN || undefined;
  if (!secretId || !secretKey) {
    throw new Error(
      "TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY are not set. Add them to .env before running this script."
    );
  }
  return { secretId: String(secretId).trim(), secretKey: String(secretKey).trim(), token };
}

export function buildTencentRequest({ secretId, secretKey, token, action, payload = {}, region = TENCENT_AI3D_REGION }) {
  const body = JSON.stringify(payload);
  const timestamp = Math.floor(Date.now() / 1000);
  const date = new Date(timestamp * 1000).toISOString().slice(0, 10);
  const contentType = "application/json; charset=utf-8";
  const canonicalHeaders = `content-type:${contentType}\nhost:${TENCENT_AI3D_HOST}\n`;
  const signedHeaders = "content-type;host";
  const canonicalRequest = ["POST", "/", "", canonicalHeaders, signedHeaders, sha256Hex(body)].join("\n");
  const credentialScope = `${date}/${TENCENT_AI3D_SERVICE}/tc3_request`;
  const stringToSign = ["TC3-HMAC-SHA256", String(timestamp), credentialScope, sha256Hex(canonicalRequest)].join("\n");
  const secretDate = hmac(`TC3${secretKey}`, date);
  const secretService = hmac(secretDate, TENCENT_AI3D_SERVICE);
  const secretSigning = hmac(secretService, "tc3_request");
  const signature = hmacHex(secretSigning, stringToSign);
  const headers = {
    "Content-Type": contentType,
    Host: TENCENT_AI3D_HOST,
    Authorization: `TC3-HMAC-SHA256 Credential=${secretId}/${credentialScope}, SignedHeaders=${signedHeaders}, Signature=${signature}`,
    "X-TC-Action": action,
    "X-TC-Timestamp": String(timestamp),
    "X-TC-Version": TENCENT_AI3D_VERSION,
    "X-TC-Region": region
  };
  if (token) headers["X-TC-Token"] = token;
  return {
    url: `https://${TENCENT_AI3D_HOST}`,
    init: { method: "POST", headers, body },
    meta: { action, region, timestamp, date, model: payload.Model, generate_type: payload.GenerateType }
  };
}

async function callTencent({ action, payload, credentials, region }) {
  const request = buildTencentRequest({ ...credentials, action, payload, region });
  const response = await fetch(request.url, request.init);
  const text = await response.text();
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error(`Tencent returned non-JSON (HTTP ${response.status}): ${text.slice(0, 300)}`);
  }
  const result = parsed?.Response;
  if (!result) {
    throw new Error(`Tencent response is missing Response: ${text.slice(0, 300)}`);
  }
  if (result.Error) {
    throw new Error(`Tencent API error ${result.Error.Code}: ${result.Error.Message}`);
  }
  return result;
}

/**
 * Zero-cost credential probe: queries a non-existent job. An AuthFailure means the
 * signature or the key pair is wrong; any other error means auth passed.
 */
export async function checkTencentCredentials({ credentials, region } = {}) {
  const resolved = credentials || (await resolveTencentCredentials());
  try {
    const response = await callTencent({
      action: TENCENT_QUERY_ACTION,
      payload: { JobId: "0" },
      credentials: resolved,
      region: region || TENCENT_AI3D_REGION
    });
    return { ok: true, action: TENCENT_QUERY_ACTION, response: sanitizeForMetadata(response) };
  } catch (error) {
    const message = String(error?.message || error);
    const authFailure = /AuthFailure|Signature|SecretId|InvalidCredential|UnauthorizedOperation|Token/i.test(message);
    return { ok: !authFailure, auth_failure: authFailure, action: TENCENT_QUERY_ACTION, error: message };
  }
}

async function readImageBase64(imagePath) {
  if (!(await pathExists(imagePath))) {
    throw new Error(`Input file does not exist: ${imagePath}`);
  }
  const info = await stat(imagePath);
  if (info.size > TENCENT_MAX_IMAGE_BYTES) {
    throw new Error(
      `Input image is ${info.size} bytes, above the Tencent limit of ${TENCENT_MAX_IMAGE_BYTES} bytes: ${imagePath}`
    );
  }
  const extension = path.extname(imagePath).toLowerCase();
  if (!TENCENT_IMAGE_EXTENSIONS.has(extension)) {
    throw new Error(
      `Tencent accepts ${[...TENCENT_IMAGE_EXTENSIONS].join("/")} images, got "${extension || "(no extension)"}": ${imagePath}`
    );
  }
  return { base64: (await readFile(imagePath)).toString("base64"), bytes: info.size };
}

function buildSubmitPayload({ base64, model, generateType, faceCount, enablePbr, polygonType, resultFormat }) {
  const payload = {
    ImageBase64: base64,
    Model: model,
    GenerateType: generateType,
    EnablePBR: Boolean(enablePbr),
    ResultFormat: resultFormat
  };
  if (Number.isFinite(faceCount) && faceCount > 0) {
    payload.FaceCount = Math.round(faceCount);
  }
  if (generateType === "LowPoly") {
    payload.PolygonType = polygonType;
  }
  return payload;
}

/**
 * Express (Rapid) tier payload: no Model, no GenerateType, no FaceCount — the service
 * decides the mesh density, which is why it is the cheap tier.
 */
function buildRapidSubmitPayload({ base64, enablePbr, resultFormat, enableGeometry = false }) {
  const payload = {
    ImageBase64: base64,
    ResultFormat: resultFormat,
    EnablePBR: Boolean(enablePbr)
  };
  if (enableGeometry) payload.EnableGeometry = true;
  return payload;
}

export function pickTencentModelFile(files) {  const list = (Array.isArray(files) ? files : []).filter((entry) => entry && entry.Url);
  if (!list.length) {
    throw new Error("Tencent reported DONE but returned no files (ResultFile3Ds is empty).");
  }
  const typeOf = (entry) => String(entry.Type || "").toUpperCase();
  const glb =
    list.find((entry) => typeOf(entry) === "GLB" && !/\.zip(\?|$)/i.test(entry.Url)) ||
    list.find((entry) => /\.glb(\?|$)/i.test(entry.Url)) ||
    list.find((entry) => typeOf(entry) === "GLB");
  if (glb) return glb;

  const archive = list.find((entry) => /\.zip(\?|$)/i.test(entry.Url) || typeOf(entry) === "OBJ");
  if (archive) {
    throw new Error(
      `Tencent returned a ${typeOf(archive)} archive (${archive.Url}) instead of a GLB. ` +
        "Pass --result-format GLB (the default) or unzip the archive by hand."
    );
  }
  throw new Error(`No GLB file in the Tencent result. Formats returned: ${list.map(typeOf).join(", ") || "(none)"}`);
}

async function pollTencentJob({ jobId, credentials, region, pollIntervalMs, timeoutMs, onStatus, queryAction = TENCENT_QUERY_ACTION }) {
  const deadline = Date.now() + timeoutMs;
  let lastStatus = "";
  for (;;) {
    const response = await callTencent({
      action: queryAction,
      payload: { JobId: jobId },
      credentials,
      region
    });
    const status = String(response.Status || "").toUpperCase();
    if (status !== lastStatus) {
      lastStatus = status;
      onStatus?.(status);
    }
    if (status === "DONE") return response;
    if (status === "FAIL") {
      throw new Error(`Tencent job ${jobId} failed: ${response.ErrorCode || "unknown"} ${response.ErrorMessage || ""}`.trim());
    }
    if (Date.now() > deadline) {
      throw new Error(
        `Tencent job ${jobId} is still ${status || "unknown"} after ${Math.round(timeoutMs / 60000)} minutes. ` +
          `The JobId stays valid for 24 hours — re-query it later with ${queryAction}.`
      );
    }
    await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));
  }
}

/**
 * Same contract as runHunyuan3D(): returns the request summary whose downloaded_files[]
 * holds the generated model, so generate-single-asset.mjs can normalize and index it.
 */
export async function runHunyuan3DTencent(options = {}) {
  const {
    image,
    outputDir,
    assetName,
    faceCount = DEFAULT_HUNYUAN_FACE_COUNT,
    enablePbr = DEFAULT_HUNYUAN_ENABLE_PBR,
    generateType = DEFAULT_HUNYUAN_GENERATE_TYPE,
    polygonType = DEFAULT_HUNYUAN_POLYGON_TYPE,
    rapid = false,
    enableGeometry = false,
    metadataPath,
    metadata = {},
    model = DEFAULT_TENCENT_3D_MODEL,
    resultFormat = DEFAULT_TENCENT_RESULT_FORMAT,
    pollIntervalMs = DEFAULT_TENCENT_POLL_INTERVAL_MS,
    timeoutMinutes = DEFAULT_TENCENT_TIMEOUT_MINUTES,
    region = TENCENT_AI3D_REGION,
    credentials,
    onStatus,
    onSubmitted
  } = options;

  if (!image) throw new Error("runHunyuan3DTencent requires an image path.");
  if (!outputDir) throw new Error("runHunyuan3DTencent requires an outputDir.");

  const rapidMode = parseBoolean(rapid, false);
  const resolvedModel = rapidMode ? null : normalizeModel(model);
  const resolvedGenerateType = rapidMode ? null : normalizeGenerateType(generateType);
  const resolvedPolygonType = rapidMode ? null : normalizePolygonType(polygonType);
  const resolvedResultFormat = normalizeResultFormat(
    resultFormat,
    rapidMode ? TENCENT_RAPID_RESULT_FORMATS : TENCENT_RESULT_FORMATS
  );
  const submitAction = rapidMode ? TENCENT_RAPID_SUBMIT_ACTION : TENCENT_SUBMIT_ACTION;
  const queryAction = rapidMode ? TENCENT_RAPID_QUERY_ACTION : TENCENT_QUERY_ACTION;
  const endpoint = rapidMode ? TENCENT_AI3D_RAPID_ENDPOINT : TENCENT_AI3D_ENDPOINT;
  const resolvedEnablePbr = parseBoolean(enablePbr, DEFAULT_HUNYUAN_ENABLE_PBR);
  const tier = rapidMode ? "rapid" : "pro";
  if (!rapidMode && resolvedModel === "3.1" && resolvedGenerateType === "LowPoly") {
    throw new Error('Tencent model "3.1" does not support GenerateType "LowPoly". Use model 3.0 or another generate type.');
  }

  const creds = credentials || (await resolveTencentCredentials());
  const index = Number.isFinite(Number(metadata.index)) ? Number(metadata.index) : 0;
  const baseProviderSlug = metadata.provider_slug || (rapidMode ? DEFAULT_TENCENT_RAPID_PROVIDER_SLUG : DEFAULT_TENCENT_PROVIDER_SLUG);
  const providerSlug = rapidMode && !baseProviderSlug.endsWith("-rapid") ? `${baseProviderSlug}-rapid` : baseProviderSlug;
  const requestMetadataPath = metadataPath || requestPath(outputDir, index, providerSlug);

  await ensureDir(outputDir);
  const { base64, bytes } = await readImageBase64(image);
  const payload = rapidMode
    ? buildRapidSubmitPayload({
        base64,
        enablePbr: resolvedEnablePbr,
        resultFormat: resolvedResultFormat,
        enableGeometry: parseBoolean(enableGeometry, false)
      })
    : buildSubmitPayload({
        base64,
        model: resolvedModel,
        generateType: resolvedGenerateType,
        faceCount: parseNumber(faceCount, DEFAULT_HUNYUAN_FACE_COUNT),
        enablePbr: resolvedEnablePbr,
        polygonType: resolvedPolygonType,
        resultFormat: resolvedResultFormat
      });
  const payloadForMetadata = {
    ...payload,
    ImageBase64: `[stripped ${bytes} bytes]`
  };

  onStatus?.(
    `submitting ${submitAction} (${tier} tier, region ${region}${rapidMode ? "" : `, model ${resolvedModel} / ${resolvedGenerateType}`})`
  );
  const submittedAt = new Date().toISOString();
  const submitResponse = await callTencent({
    action: submitAction,
    payload,
    credentials: creds,
    region
  });
  const jobId = submitResponse.JobId;
  if (!jobId) {
    throw new Error(`Tencent did not return a JobId: ${JSON.stringify(sanitizeForMetadata(submitResponse))}`);
  }
  onSubmitted?.({ jobId, submitted_at: submittedAt });
  onStatus?.(`job ${jobId} submitted`);

  // In-flight metadata deliberately omits request_id/endpoint: those two fields make
  // generate-single-asset.mjs treat the record as a resumable *fal* request.
  await writeJson(requestMetadataPath, {
    schema_version: 1,
    kind: "3d",
    ...metadata,
    provider: endpoint,
    provider_slug: providerSlug,
    status: "running",
    tier,
    job_id: jobId,
    submitted_at: submittedAt,
    input_files: [image],
    input: payloadForMetadata
  });

  const queryResponse = await pollTencentJob({
    jobId,
    credentials: creds,
    region,
    queryAction,
    pollIntervalMs: parseNumber(pollIntervalMs, DEFAULT_TENCENT_POLL_INTERVAL_MS),
    timeoutMs: parseNumber(timeoutMinutes, DEFAULT_TENCENT_TIMEOUT_MINUTES) * 60 * 1000,
    onStatus
  });

  const modelFile = pickTencentModelFile(queryResponse.ResultFile3Ds);
  const modelFileName = `${index}-${safeFileName(providerSlug)}-${safeFileName(slugify(assetName || "model") || "model")}.glb`;
  const modelPath = path.join(outputDir, modelFileName);
  await downloadFile(modelFile.Url, modelPath);
  onStatus?.(`downloaded ${path.basename(modelPath)}`);

  let previewPath;
  if (modelFile.PreviewImageUrl) {
    // Hidden dot-file: normalizeModelFiles() must only ever see the model, and a
    // visible <index>-<slug>.png would collide with the object reference image.
    previewPath = path.join(outputDir, `.${index}-${safeFileName(providerSlug)}-preview.png`);
    try {
      await downloadFile(modelFile.PreviewImageUrl, previewPath);
    } catch (error) {
      previewPath = undefined;
      onStatus?.(`preview download failed: ${error.message}`);
    }
  }

  const creditDetails = queryResponse.ResultCreditDetails;
  const creditConsumed = queryResponse.ResultCreditConsumed;
  const downloadedFiles = [{ label: "glb", path: modelPath, source: modelFile.Url }];
  if (previewPath) downloadedFiles.push({ label: "preview", path: previewPath, source: modelFile.PreviewImageUrl });

  const summary = buildRequestSummary({
    kind: "3d",
    provider: endpoint,
    metadata: {
      ...metadata,
      provider_slug: providerSlug,
      tier,
      job_id: jobId,
      model: resolvedModel,
      generate_type: resolvedGenerateType,
      face_count: payload.FaceCount ?? null,
      enable_pbr: payload.EnablePBR,
      result_format: resolvedResultFormat,
      region,
      credit_details: creditDetails,
      credit_consumed: creditConsumed
    },
    requestId: jobId,
    submittedAt,
    inputFiles: [image],
    outputFiles: [modelPath],
    downloadedFiles,
    result: {
      status: queryResponse.Status,
      request_id: queryResponse.RequestId,
      files: sanitizeForMetadata(queryResponse.ResultFile3Ds || []),
      credit_details: creditDetails,
      credit_consumed: creditConsumed
    },
    extra: { asset_name: assetName, input: payloadForMetadata }
  });

  await writeJson(requestMetadataPath, summary);
  onStatus?.(
    `job ${jobId} done${creditConsumed !== undefined ? ` — ${creditConsumed} credits consumed` : ""}`
  );
  return summary;
}

async function main() {
  const { flags } = parseArgs();
  await loadDotEnv();

  const check = parseBoolean(one(flags, "check", false), false) || flags.check === true;
  const dryRun = parseBoolean(one(flags, "dry-run", false), false) || flags["dry-run"] === true;
  const region = one(flags, "region", TENCENT_AI3D_REGION);

  if (check) {
    const result = await checkTencentCredentials({ region });
    console.log(JSON.stringify(result, null, 2));
    if (!result.ok) process.exitCode = 1;
    return;
  }

  const image = one(flags, "image", one(flags, "input-image", undefined));
  const outputDir = one(flags, "output-dir", undefined);
  const assetName = one(flags, "asset-name", one(flags, "name", "model"));
  const model = one(flags, "model", one(flags, "tencent-model", DEFAULT_TENCENT_3D_MODEL));
  const generateType = one(flags, "generate-type", DEFAULT_HUNYUAN_GENERATE_TYPE);
  const polygonType = one(flags, "polygon-type", DEFAULT_HUNYUAN_POLYGON_TYPE);
  const resultFormat = one(flags, "result-format", DEFAULT_TENCENT_RESULT_FORMAT);
  const faceCount = one(flags, "face-count", String(DEFAULT_HUNYUAN_FACE_COUNT));
  const enablePbr = one(flags, "enable-pbr", String(DEFAULT_HUNYUAN_ENABLE_PBR));
  const index = one(flags, "index", "0");
  const rapid = parseBoolean(one(flags, "rapid", false), false) || flags.rapid === true;
  const enableGeometry =
    parseBoolean(one(flags, "enable-geometry", false), false) || flags["enable-geometry"] === true;

  if (!image) {
    console.error(
      [
        "Usage:",
        "  node .claude/scripts/asset-pipeline/hunyuan-3d-tencent.mjs \\",
        "    --image <input.png> --output-dir <dir> --asset-name <name> [options]",
        "",
        "Options:",
        "  --check                      Verify credentials with a zero-cost query, then exit",
        "  --dry-run                    Print the request that would be sent, spend nothing",
        "  --rapid                      Express tier (SubmitHunyuanTo3DRapidJob): much cheaper,",
        "                               ignores --model/--generate-type/--face-count",
        "  --enable-geometry            (rapid only) ask the service for geometry only",
        "  --model 3.0|3.1              Tencent model version (default 3.0, pro tier only)",
        "  --generate-type Normal|LowPoly|Geometry|Sketch   (default Normal)",
        "  --polygon-type triangle|quadrilateral            (LowPoly only)",
        "  --face-count <n|0>           Mesh faces; 0 omits FaceCount (server default 500000, saves credits)",
        "  --enable-pbr true|false      PBR material generation (default true, costs extra credits)",
        "  --result-format GLB|OBJ|STL|USDZ|FBX             (default GLB)",
        "  --region <region>            Only ap-guangzhou is supported by ai3d",
        "  --index <n>                  Artifact index used for metadata/filenames",
        "  --poll-interval-ms <n> --timeout-minutes <n>"
      ].join("\n")
    );
    process.exitCode = 1;
    return;
  }

  if (!outputDir) {
    throw new Error("--output-dir is required.");
  }

  if (dryRun) {
    const credentials = await resolveTencentCredentials();
    const { base64, bytes } = await readImageBase64(image);
    const payload = rapid
      ? buildRapidSubmitPayload({
          base64,
          enablePbr: parseBoolean(enablePbr, DEFAULT_HUNYUAN_ENABLE_PBR),
          resultFormat: normalizeResultFormat(resultFormat, TENCENT_RAPID_RESULT_FORMATS),
          enableGeometry
        })
      : buildSubmitPayload({
          base64,
          model: normalizeModel(model),
          generateType: normalizeGenerateType(generateType),
          faceCount: parseNumber(faceCount, DEFAULT_HUNYUAN_FACE_COUNT),
          enablePbr: parseBoolean(enablePbr, DEFAULT_HUNYUAN_ENABLE_PBR),
          polygonType: normalizePolygonType(polygonType),
          resultFormat: normalizeResultFormat(resultFormat)
        });
    const action = rapid ? TENCENT_RAPID_SUBMIT_ACTION : TENCENT_SUBMIT_ACTION;
    const request = buildTencentRequest({
      ...credentials,
      action,
      payload: { ...payload, ImageBase64: `[${bytes} bytes]` },
      region
    });
    console.log(
      JSON.stringify(
        {
          dry_run: true,
          tier: rapid ? "rapid" : "pro",
          url: request.url,
          signed_headers: request.init.headers.Authorization
            .replace(/Credential=[^/]+\//, "Credential=<redacted>/")
            .replace(/Signature=.*/, "Signature=<redacted>"),
          action,
          region,
          payload: { ...payload, ImageBase64: `[${bytes} bytes]` }
        },
        null,
        2
      )
    );
    return;
  }

  const summary = await runHunyuan3DTencent({
    image,
    outputDir,
    assetName,
    metadata: { index: Number(index) || 0 },
    model,
    generateType,
    polygonType,
    rapid,
    enableGeometry,
    resultFormat,
    faceCount: parseNumber(faceCount, DEFAULT_HUNYUAN_FACE_COUNT),
    enablePbr: parseBoolean(enablePbr, DEFAULT_HUNYUAN_ENABLE_PBR),
    region,
    onStatus: (status) => console.error(`[hunyuan-tencent] ${status}`)
  });
  console.log(JSON.stringify(summary, null, 2));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
}
