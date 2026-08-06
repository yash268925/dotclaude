#!/usr/bin/env bun
/**
 * Claude Code の subagent (Task tool) 単位でトークン使用量・コストを集計するレポーター。
 *
 * ccusage はセッション（会話）単位までしか集計できないため、
 * ~/.claude/projects/ ** / *.jsonl と、その下の <sessionId>/subagents/ *.jsonl
 * (+ *.meta.json の agentType) を直接パースして、サブエージェント種別ごとに
 * トークン使用量を出す。
 *
 * コストは自前で単価を持たずに、ccusage の当該セッション・モデルの実コストを
 * 「重み付きトークン数」の比率で agentType に配分する方式で算出する
 * (重み = input + 5*output + 1.25*cache_write + 0.1*cache_read。
 *  Anthropic の公開料金表に共通する input:output:cache_write:cache_read の
 *  標準的な倍率をそのまま使っている)。
 * これにより agentType 別コストの合計は、必ず ccusage が報告する実コストと一致する。
 *
 * 使い方:
 *     agent-usage-report.ts [YYYY-MM-DD] [--id SESSION_ID_PREFIX]
 *         # 日付省略時は本日 (JST)
 *         # --id は session_id の前方一致フィルタ。レポートの [leaf:xxxxxxxx] や
 *         # statusline 右端に出る8桁ハッシュをそのまま渡せる。
 *         # --id 指定時は、同一 agentType のサブエージェントを合算せず
 *         # agentType#N として個別行で表示する。
 */
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join } from "node:path";
import { spawnSync } from "node:child_process";

const PROJECTS_DIR = join(homedir(), ".claude", "projects");
const JST_OFFSET_MS = 9 * 60 * 60 * 1000;

const USAGE = `usage: agent-usage-report.ts [-h] [--id SESSION_ID_PREFIX] [date]

Claude Code の subagent 単位でトークン使用量・コストを集計するレポーター

positional arguments:
  date                  対象日 YYYY-MM-DD (省略時は本日 JST)

options:
  -h, --help            show this help message and exit
  --id SESSION_ID_PREFIX
                        session_id の前方一致でフィルタ (8桁ハッシュなど部分指定可)`;

type Args = { date: string | null; sessionIdFilter: string | null };

function die(message: string): never {
  console.error(`error: ${message}`);
  console.error(USAGE);
  process.exit(1);
}

function parseArgs(argv: string[]): Args {
  let date: string | null = null;
  let sessionIdFilter: string | null = null;

  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i]!;
    if (arg === "-h" || arg === "--help") {
      console.log(USAGE);
      process.exit(0);
    } else if (arg === "--id") {
      const value = argv[++i];
      if (value === undefined) die("argument --id: expected one argument");
      sessionIdFilter = value;
    } else if (arg.startsWith("--id=")) {
      sessionIdFilter = arg.slice("--id=".length);
    } else if (arg.startsWith("-") && arg !== "-") {
      die(`unrecognized argument: ${arg}`);
    } else if (date === null) {
      date = arg;
    } else {
      die(`unrecognized argument: ${arg}`);
    }
  }
  return { date, sessionIdFilter };
}

/** JST における年月日。Intl は使わず、UTC にオフセットを加算して取り出す。 */
function jstYMD(epochMs: number): [number, number, number] {
  const d = new Date(epochMs + JST_OFFSET_MS);
  return [d.getUTCFullYear(), d.getUTCMonth() + 1, d.getUTCDate()];
}

function parseTargetDate(dateStr: string | null): [number, number, number] {
  if (dateStr) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateStr);
    if (!m) die(`argument date: invalid date format: ${dateStr} (expected YYYY-MM-DD)`);
    const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
    const probe = new Date(Date.UTC(y, mo - 1, d));
    if (
      probe.getUTCFullYear() !== y ||
      probe.getUTCMonth() + 1 !== mo ||
      probe.getUTCDate() !== d
    ) {
      die(`argument date: invalid date: ${dateStr}`);
    }
    return [y, mo, d];
  }
  return jstYMD(Date.now());
}

function formatDate([y, m, d]: [number, number, number]): string {
  return `${String(y).padStart(4, "0")}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}

type ModelBreakdown = { modelName: string; cost: number };

/** sessionId -> modelBreakdowns( [{modelName, cost, inputTokens, ...}, ...] ) */
function loadCcusageSessionCosts(): Map<string, ModelBreakdown[]> {
  const res = spawnSync("ccusage", ["session", "--json"], { encoding: "utf8" });
  if (res.error) {
    const code = (res.error as NodeJS.ErrnoException).code;
    if (code === "ENOENT") {
      console.error("error: 'ccusage' コマンドが見つかりません。インストールして PATH を通してください。");
      process.exit(1);
    }
    console.error(`error: ccusage の実行に失敗しました: ${res.error.message}`);
    process.exit(1);
  }
  if (res.status !== 0) {
    console.error(`error: ccusage session --json が exit code ${res.status} で失敗しました`);
    if (res.stderr) console.error(res.stderr.trimEnd());
    process.exit(1);
  }
  let data: { session: { period: string; modelBreakdowns: ModelBreakdown[] }[] };
  try {
    data = JSON.parse(res.stdout);
  } catch {
    console.error("error: ccusage の出力を JSON として解析できませんでした");
    process.exit(1);
  }
  const map = new Map<string, ModelBreakdown[]>();
  for (const s of data.session) map.set(s.period, s.modelBreakdowns);
  return map;
}

type Usage = Record<string, number | null | undefined>;
type Entry = {
  ts: string | null;
  requestId: string | null;
  usage: Usage;
  model: string | null;
  effort: string | null;
};

/**
 * (timestamp, requestId, usage dict, model, effort) のリストを返す。
 *
 * skipSidechain=true はメインセッションファイル用。旧バージョンの Claude Code は
 * subagent の発言をメインの transcript にも isSidechain=true で埋め込んでいたため、
 * 二重計上を避けるために除外する。一方、独立した subagents/ *.jsonl は全行が
 * isSidechain=true なので、そちらでは skipSidechain=false にすること。
 */
function readUsageEntries(path: string, skipSidechain: boolean): Entry[] {
  const entries: Entry[] = [];
  let text: string;
  try {
    text = readFileSync(path, "utf8");
  } catch {
    return entries;
  }
  for (let line of text.split("\n")) {
    line = line.trim();
    if (!line) continue;
    let d: any;
    try {
      d = JSON.parse(line);
    } catch {
      continue;
    }
    if (skipSidechain && d.isSidechain) continue;
    const msg = d.message;
    if (typeof msg !== "object" || msg === null || Array.isArray(msg)) continue;
    const usage = msg.usage;
    if (!usage) continue;
    entries.push({
      ts: d.timestamp ?? null,
      requestId: d.requestId ?? null,
      usage,
      model: msg.model ?? null,
      effort: d.effort ?? null,
    });
  }
  return entries;
}

/**
 * 同一 requestId の行はストリーミング途中経過の重複行なので、
 * 最後(=最終確定 usage)の行だけを残す。
 */
function dedupLast(entries: Entry[]): Entry[] {
  const last = new Map<string, Entry>();
  entries.forEach((e, i) => {
    const key = e.requestId ? e.requestId : `__norid_${i}`;
    last.set(key, e); // Map は挿入順を保つので order の別管理は不要
  });
  return [...last.values()];
}

/**
 * 'claude-opus-5' + 'high' -> 'opus-5(high)' のように、
 * モデル名を短縮し effort を括弧書きで添える。
 */
function shortModelLabel(model: string, effort: string | null): string {
  let short = model;
  if (short.startsWith("claude-")) short = short.slice("claude-".length);
  short = short.replace(/-\d{8}$/, ""); // 日付サフィックス (例: -20251001) を除去
  return effort ? `${short}(${effort})` : short;
}

function inDate(tsIso: string | null, targetDate: [number, number, number]): boolean {
  if (!tsIso) return false;
  const ms = Date.parse(tsIso);
  if (Number.isNaN(ms)) return false;
  const [y, m, d] = jstYMD(ms);
  return y === targetDate[0] && m === targetDate[1] && d === targetDate[2];
}

function num(usage: Usage, key: string): number {
  return usage[key] || 0;
}

function weight(usage: Usage): number {
  const i = num(usage, "input_tokens");
  const o = num(usage, "output_tokens");
  const cw = num(usage, "cache_creation_input_tokens");
  const cr = num(usage, "cache_read_input_tokens");
  return i + 5 * o + 1.25 * cw + 0.1 * cr;
}

type Tokens = { input: number; output: number; cache_write: number; cache_read: number };
const TOKEN_KEYS = ["input", "output", "cache_write", "cache_read"] as const;

function tok(usage: Usage): Tokens {
  return {
    input: num(usage, "input_tokens"),
    output: num(usage, "output_tokens"),
    cache_write: num(usage, "cache_creation_input_tokens"),
    cache_read: num(usage, "cache_read_input_tokens"),
  };
}

/**
 * '-Users-foxtail-work-ToripoAndroid' -> 'ToripoAndroid' のように
 * プロジェクトディレクトリのエンコード名から末尾の一意な名前を取り出す。
 */
function projectLeaf(projectDirName: string): string {
  const parts = projectDirName.split("-").filter((p) => p);
  return parts.length ? parts[parts.length - 1]! : projectDirName;
}

/**
 * main・サブエージェントいずれもセッションを跨いでは合算しない。
 * ただし同一セッション内で同じ agentType を複数回呼び出した分は合算する
 * (tokensToday 側が (sessionId, agentType) をキーにしているため、
 * ここに来る時点で既に合算済み)。
 */
function displayKey(agentType: string, sessionId: string, leaf: string): string {
  return `${agentType} [${leaf}:${sessionId.slice(0, 8)}]`;
}

function listDir(dir: string): string[] {
  try {
    return readdirSync(dir);
  } catch {
    return [];
  }
}

/** glob(PROJECTS_DIR/ * / *.jsonl) 相当。glob と同じくドット始まりは除外する。 */
function findSessionFiles(): string[] {
  const files: string[] = [];
  for (const project of listDir(PROJECTS_DIR)) {
    if (project.startsWith(".")) continue;
    const projectDir = join(PROJECTS_DIR, project);
    for (const name of listDir(projectDir)) {
      if (name.startsWith(".") || !name.endsWith(".jsonl")) continue;
      files.push(join(projectDir, name));
    }
  }
  return files;
}

function isDir(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

/** ネストした Map から値を取り出す (無ければ生成)。 */
function getOr<K, V>(map: Map<K, V>, key: K, make: () => V): V {
  let v = map.get(key);
  if (v === undefined) {
    v = make();
    map.set(key, v);
  }
  return v;
}

function main(): void {
  const args = parseArgs(process.argv.slice(2));
  const targetDate = parseTargetDate(args.date);
  const idFilter = args.sessionIdFilter ? args.sessionIdFilter.toLowerCase() : null;
  const ccusageCosts = loadCcusageSessionCosts();

  let sessionFiles = findSessionFiles();
  if (idFilter) {
    sessionFiles = sessionFiles.filter((sf) =>
      basename(sf).slice(0, -".jsonl".length).toLowerCase().startsWith(idFilter),
    );
  }

  // tokensToday[sessionId\0agentType][model\0effort] = {input,output,cache_write,cache_read}
  const tokensToday = new Map<string, Map<string, Tokens>>();
  // sessionModelTotalWeight[sessionId][model] = 全期間の重み合計 (コスト配分の分母)
  const sessionModelTotalWeight = new Map<string, Map<string, number>>();
  // sessionModelAgentWeightToday[sessionId][model][agentType] = 本日分の重み (分子)
  const sessionModelAgentWeightToday = new Map<string, Map<string, Map<string, number>>>();
  // leafOf[sessionId] = プロジェクト名 (表示用)
  const leafOf = new Map<string, string>();

  const missingCostSessions = new Set<string>();

  for (const sf of sessionFiles) {
    const sessionId = basename(sf).slice(0, -".jsonl".length);
    leafOf.set(sessionId, projectLeaf(basename(dirname(sf))));
    const subdir = join(dirname(sf), sessionId, "subagents");
    const sources: [string, string][] = [["main", sf]];
    if (isDir(subdir)) {
      // 起動順に近い順序で番号を振るため、meta の更新時刻でソートする。
      const metaPaths = listDir(subdir)
        .filter((n) => !n.startsWith(".") && n.endsWith(".meta.json"))
        .map((n) => join(subdir, n))
        .map((p) => ({ path: p, mtime: statSync(p).mtimeMs / 1000 }))
        .sort((a, b) => (a.mtime !== b.mtime ? a.mtime - b.mtime : a.path < b.path ? -1 : a.path > b.path ? 1 : 0))
        .map((x) => x.path);
      const seq = new Map<string, number>();
      for (const metaPath of metaPaths) {
        const agentFile = metaPath.slice(0, -".meta.json".length) + ".jsonl";
        if (!existsSync(agentFile)) continue;
        let meta: any;
        try {
          meta = JSON.parse(readFileSync(metaPath, "utf8"));
        } catch {
          meta = {};
        }
        const agentType = meta?.agentType ?? "unknown-subagent";
        seq.set(agentType, (seq.get(agentType) ?? 0) + 1);
        let label = agentType;
        if (idFilter) {
          // --id 指定時は同一 agentType の呼び出しを合算せず、個別行として表示する。
          label = `${agentType}#${seq.get(agentType)}`;
        }
        sources.push([label, agentFile]);
      }
    }

    for (const [agentType, path] of sources) {
      const deduped = dedupLast(readUsageEntries(path, agentType === "main"));
      for (const { ts, usage, model, effort } of deduped) {
        if (!model) continue;
        const w = weight(usage);
        // コスト配分は ccusage 側の単価が model 単位 (effort別ではない) なので、
        // ここは生の model 名のまま集計する。
        const perModel = getOr(sessionModelTotalWeight, sessionId, () => new Map<string, number>());
        perModel.set(model, (perModel.get(model) ?? 0) + w);
        if (inDate(ts, targetDate)) {
          const perAgent = getOr(
            getOr(sessionModelAgentWeightToday, sessionId, () => new Map()),
            model,
            () => new Map<string, number>(),
          );
          perAgent.set(agentType, (perAgent.get(agentType) ?? 0) + w);
          // 表示用のトークン内訳は model+effort 単位で分ける。
          const t = getOr(
            getOr(tokensToday, `${sessionId}\0${agentType}`, () => new Map<string, Tokens>()),
            `${model}\0${effort ?? ""}`,
            () => ({ input: 0, output: 0, cache_write: 0, cache_read: 0 }),
          );
          const add = tok(usage);
          for (const k of TOKEN_KEYS) t[k] += add[k];
        }
      }
    }
  }

  // コスト配分 (sessionId, agentType) 単位で算出
  const costToday = new Map<string, number>();
  for (const [sessionId, perModel] of sessionModelAgentWeightToday) {
    const breakdowns = ccusageCosts.get(sessionId);
    if (!breakdowns || breakdowns.length === 0) {
      missingCostSessions.add(sessionId);
      continue;
    }
    const bdByModel = new Map(breakdowns.map((b) => [b.modelName, b]));
    for (const [model, perAgentWToday] of perModel) {
      const bd = bdByModel.get(model);
      const totalW = sessionModelTotalWeight.get(sessionId)?.get(model) ?? 0;
      if (!bd || totalW === 0) continue;
      const rate = bd.cost / totalW; // $ per weighted-token, このセッション/モデル限定
      for (const [agentType, wToday] of perAgentWToday) {
        const key = `${sessionId}\0${agentType}`;
        costToday.set(key, (costToday.get(key) ?? 0) + rate * wToday);
      }
    }
  }

  // (sessionId, agentType) -> 表示用キーへ集約。
  // main はセッションごとに別行、サブエージェントは種別で合算する。
  type Row = Tokens & { cost: number; models: Set<string> };
  const display = new Map<string, Row>();
  for (const [sessionAgent, perModel] of tokensToday) {
    const sep = sessionAgent.indexOf("\0");
    const sessionId = sessionAgent.slice(0, sep);
    const agentType = sessionAgent.slice(sep + 1);
    const key = displayKey(agentType, sessionId, leafOf.get(sessionId) ?? "?");
    const row = getOr(display, key, () => ({
      input: 0,
      output: 0,
      cache_write: 0,
      cache_read: 0,
      cost: 0,
      models: new Set<string>(),
    }));
    row.cost += costToday.get(sessionAgent) ?? 0;
    for (const [modelEffort, t] of perModel) {
      const msep = modelEffort.indexOf("\0");
      const model = modelEffort.slice(0, msep);
      const effort = modelEffort.slice(msep + 1);
      row.models.add(shortModelLabel(model, effort || null));
      for (const k of TOKEN_KEYS) row[k] += t[k];
    }
  }

  // 出力
  let header = `=== Agent別トークン使用量レポート (${formatDate(targetDate)} JST) ===`;
  if (idFilter) header += `  [id filter: ${idFilter}*]`;
  console.log(header + "\n");

  if (idFilter && sessionFiles.length === 0) {
    console.log(`[warning] session_id が '${idFilter}' で始まるセッションが見つかりません`);
    return;
  }

  type OutRow = [string, number, number, number, number, number, number, string[]];
  const rows: OutRow[] = [];
  let grandTokens = 0;
  let grandCost = 0;
  for (const [key, row] of display) {
    const total = row.input + row.output + row.cache_write + row.cache_read;
    rows.push([
      key,
      row.input,
      row.output,
      row.cache_write,
      row.cache_read,
      total,
      row.cost,
      [...row.models].sort(),
    ]);
    grandTokens += total;
    grandCost += row.cost;
  }

  rows.sort((a, b) => b[5] - a[5]); // Array#sort は安定なので Python の sort と同じ順序になる

  const labelW = Math.max(44, Math.max(0, ...rows.map((r) => r[0].length)) + 2);
  const col = (
    label: string,
    i: string | number,
    o: string | number,
    cw: string | number,
    cr: string | number,
    total: string | number,
    cost: string,
    models: string,
  ) =>
    String(label).padEnd(labelW) +
    String(i).padStart(10) +
    String(o).padStart(10) +
    String(cw).padStart(12) +
    String(cr).padStart(14) +
    String(total).padStart(14) +
    cost.padStart(12) +
    "  " +
    models;
  const lineW = labelW + 10 + 10 + 12 + 14 + 14 + 12;
  console.log(col("agent", "input", "output", "cache_w", "cache_r", "total", "cost($)", "models"));
  console.log("-".repeat(lineW));
  for (const [key, i, o, cw, cr, total, cost, models] of rows) {
    console.log(col(key, i, o, cw, cr, total, cost.toFixed(4), models.join(",")));
  }
  console.log("-".repeat(lineW));
  console.log(col("TOTAL", "", "", "", "", grandTokens, grandCost.toFixed(4), ""));

  if (missingCostSessions.size > 0) {
    const repr = `{${[...missingCostSessions].map((s) => `'${s}'`).join(", ")}}`;
    console.log(`\n[warning] ccusage 側にコスト情報が見つからなかった session: ${repr}`);
  }
}

main();
