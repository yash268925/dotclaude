#!/usr/bin/env python3
"""
Claude Code の subagent (Task tool) 単位でトークン使用量・コストを集計するレポーター。

ccusage はセッション（会話）単位までしか集計できないため、
~/.claude/projects/**/*.jsonl と、その下の <sessionId>/subagents/*.jsonl
(+ *.meta.json の agentType) を直接パースして、サブエージェント種別ごとに
トークン使用量を出す。

コストは自前で単価を持たずに、ccusage の当該セッション・モデルの実コストを
「重み付きトークン数」の比率で agentType に配分する方式で算出する
(重み = input + 5*output + 1.25*cache_write + 0.1*cache_read。
 Anthropic の公開料金表に共通する input:output:cache_write:cache_read の
 標準的な倍率をそのまま使っている)。
これにより agentType 別コストの合計は、必ず ccusage が報告する実コストと一致する。

使い方:
    python3 agent-usage-report.py [YYYY-MM-DD] [--id SESSION_ID_PREFIX]
        # 日付省略時は本日 (JST)
        # --id は session_id の前方一致フィルタ。レポートの [leaf:xxxxxxxx] や
        # statusline 右端に出る8桁ハッシュをそのまま渡せる。
        # --id 指定時は、同一 agentType のサブエージェントを合算せず
        # agentType#N として個別行で表示する。
"""
import argparse
import json
import os
import re
import glob
import datetime
import subprocess
import sys
from collections import defaultdict

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
JST = datetime.timezone(datetime.timedelta(hours=9))


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Claude Code の subagent 単位でトークン使用量・コストを集計するレポーター"
    )
    parser.add_argument(
        "date", nargs="?", default=None, help="対象日 YYYY-MM-DD (省略時は本日 JST)"
    )
    parser.add_argument(
        "--id",
        dest="session_id_filter",
        default=None,
        help="session_id の前方一致でフィルタ (8桁ハッシュなど部分指定可)",
    )
    return parser.parse_args(argv[1:])


def parse_target_date(date_str):
    if date_str:
        y, m, d = map(int, date_str.split("-"))
        return datetime.date(y, m, d)
    return datetime.datetime.now(JST).date()


def load_ccusage_session_costs():
    """sessionId -> modelBreakdowns( [{modelName, cost, inputTokens, ...}, ...] )"""
    out = subprocess.run(
        ["ccusage", "session", "--json"], capture_output=True, text=True, check=True
    ).stdout
    data = json.loads(out)
    return {s["period"]: s["modelBreakdowns"] for s in data["session"]}


def read_usage_entries(path, skip_sidechain=False):
    """(timestamp, requestId, usage dict, model, effort) のリストを返す。

    skip_sidechain=True はメインセッションファイル用。旧バージョンの Claude Code は
    subagent の発言をメインの transcript にも isSidechain=true で埋め込んでいたため、
    二重計上を避けるために除外する。一方、独立した subagents/*.jsonl は全行が
    isSidechain=true なので、そちらでは skip_sidechain=False にすること。
    """
    entries = []
    try:
        f = open(path)
    except OSError:
        return entries
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if skip_sidechain and d.get("isSidechain"):
                continue
            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not usage:
                continue
            entries.append((d.get("timestamp"), d.get("requestId"), usage, msg.get("model"), d.get("effort")))
    return entries


def dedup_last(entries):
    """同一 requestId の行はストリーミング途中経過の重複行なので、
    最後(=最終確定 usage)の行だけを残す。"""
    last = {}
    order = []
    for i, (ts, rid, usage, model, effort) in enumerate(entries):
        key = rid if rid else f"__norid_{i}"
        if key not in last:
            order.append(key)
        last[key] = (ts, usage, model, effort)
    return [last[k] for k in order]


def short_model_label(model, effort):
    """'claude-opus-5' + 'high' -> 'opus-5(high)' のように、
    モデル名を短縮し effort を括弧書きで添える。"""
    short = model
    if short.startswith("claude-"):
        short = short[len("claude-"):]
    short = re.sub(r"-\d{8}$", "", short)  # 日付サフィックス (例: -20251001) を除去
    if effort:
        return f"{short}({effort})"
    return short


def in_date(ts_iso, target_date):
    if not ts_iso:
        return False
    dt = datetime.datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).astimezone(JST)
    return dt.date() == target_date


def weight(usage):
    i = usage.get("input_tokens", 0) or 0
    o = usage.get("output_tokens", 0) or 0
    cw = usage.get("cache_creation_input_tokens", 0) or 0
    cr = usage.get("cache_read_input_tokens", 0) or 0
    return i + 5 * o + 1.25 * cw + 0.1 * cr


def tok(usage):
    return {
        "input": usage.get("input_tokens", 0) or 0,
        "output": usage.get("output_tokens", 0) or 0,
        "cache_write": usage.get("cache_creation_input_tokens", 0) or 0,
        "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
    }


def project_leaf(project_dir_name):
    """'-Users-foxtail-work-ToripoAndroid' -> 'ToripoAndroid' のように
    プロジェクトディレクトリのエンコード名から末尾の一意な名前を取り出す。"""
    parts = [p for p in project_dir_name.split("-") if p]
    return parts[-1] if parts else project_dir_name


def display_key(agent_type, session_id, leaf):
    """main・サブエージェントいずれもセッションを跨いでは合算しない。
    ただし同一セッション内で同じ agentType を複数回呼び出した分は合算する
    (tokens_today 側が (session_id, agent_type) をキーにしているため、
    ここに来る時点で既に合算済み)。"""
    return f"{agent_type} [{leaf}:{session_id[:8]}]"


def main():
    args = parse_args(sys.argv)
    target_date = parse_target_date(args.date)
    id_filter = args.session_id_filter.lower() if args.session_id_filter else None
    ccusage_costs = load_ccusage_session_costs()

    session_files = glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl"))
    if id_filter:
        session_files = [
            sf for sf in session_files
            if os.path.basename(sf)[:-len(".jsonl")].lower().startswith(id_filter)
        ]

    # tokens_today[(sessionId, agentType)][(model, effort)] = {input,output,cache_write,cache_read}
    tokens_today = defaultdict(lambda: defaultdict(lambda: {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}))
    # session_model_total_weight[sessionId][model] = 全期間の重み合計 (コスト配分の分母)
    session_model_total_weight = defaultdict(lambda: defaultdict(float))
    # session_model_agent_weight_today[sessionId][model][agentType] = 本日分の重み (分子)
    session_model_agent_weight_today = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    # leaf_of[sessionId] = プロジェクト名 (表示用)
    leaf_of = {}

    missing_cost_sessions = set()

    for sf in session_files:
        session_id = os.path.basename(sf)[:-len(".jsonl")]
        leaf_of[session_id] = project_leaf(os.path.basename(os.path.dirname(sf)))
        subdir = os.path.join(os.path.dirname(sf), session_id, "subagents")
        sources = [("main", sf)]
        if os.path.isdir(subdir):
            # 起動順に近い順序で番号を振るため、meta の更新時刻でソートする。
            meta_paths = sorted(
                glob.glob(os.path.join(subdir, "*.meta.json")),
                key=lambda p: (os.path.getmtime(p), p),
            )
            seq = defaultdict(int)
            for meta_path in meta_paths:
                agent_file = meta_path[: -len(".meta.json")] + ".jsonl"
                if not os.path.exists(agent_file):
                    continue
                try:
                    meta = json.load(open(meta_path))
                except Exception:
                    meta = {}
                agent_type = meta.get("agentType", "unknown-subagent")
                seq[agent_type] += 1
                label = agent_type
                if id_filter:
                    # --id 指定時は同一 agentType の呼び出しを合算せず、個別行として表示する。
                    label = f"{agent_type}#{seq[agent_type]}"
                sources.append((label, agent_file))

        for agent_type, path in sources:
            deduped = dedup_last(read_usage_entries(path, skip_sidechain=(agent_type == "main")))
            for ts, usage, model, effort in deduped:
                if not model:
                    continue
                w = weight(usage)
                # コスト配分は ccusage 側の単価が model 単位 (effort別ではない) なので、
                # ここは生の model 名のまま集計する。
                session_model_total_weight[session_id][model] += w
                if in_date(ts, target_date):
                    session_model_agent_weight_today[session_id][model][agent_type] += w
                    # 表示用のトークン内訳は model+effort 単位で分ける。
                    t = tokens_today[(session_id, agent_type)][(model, effort)]
                    for k, v in tok(usage).items():
                        t[k] += v

    # コスト配分 (session_id, agentType) 単位で算出
    cost_today = defaultdict(float)
    for session_id, per_model in session_model_agent_weight_today.items():
        breakdowns = ccusage_costs.get(session_id)
        if not breakdowns:
            missing_cost_sessions.add(session_id)
            continue
        bd_by_model = {b["modelName"]: b for b in breakdowns}
        for model, per_agent_w_today in per_model.items():
            bd = bd_by_model.get(model)
            total_w = session_model_total_weight[session_id][model]
            if not bd or total_w == 0:
                continue
            rate = bd["cost"] / total_w  # $ per weighted-token, このセッション/モデル限定
            for agent_type, w_today in per_agent_w_today.items():
                cost_today[(session_id, agent_type)] += rate * w_today

    # (session_id, agentType) -> 表示用キーへ集約。
    # main はセッションごとに別行、サブエージェントは種別で合算する。
    display = defaultdict(lambda: {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "cost": 0.0, "models": set()})
    for (session_id, agent_type), per_model in tokens_today.items():
        key = display_key(agent_type, session_id, leaf_of.get(session_id, "?"))
        row = display[key]
        row["cost"] += cost_today.get((session_id, agent_type), 0.0)
        for (model, effort), t in per_model.items():
            row["models"].add(short_model_label(model, effort))
            for k in ("input", "output", "cache_write", "cache_read"):
                row[k] += t[k]

    # 出力
    header = f"=== Agent別トークン使用量レポート ({target_date} JST) ==="
    if id_filter:
        header += f"  [id filter: {id_filter}*]"
    print(header + "\n")

    if id_filter and not session_files:
        print(f"[warning] session_id が '{id_filter}' で始まるセッションが見つかりません")
        return
    rows = []
    grand_tokens = 0
    grand_cost = 0.0
    for key, row in display.items():
        total = row["input"] + row["output"] + row["cache_write"] + row["cache_read"]
        rows.append((key, row["input"], row["output"], row["cache_write"], row["cache_read"], total, row["cost"], sorted(row["models"])))
        grand_tokens += total
        grand_cost += row["cost"]

    rows.sort(key=lambda r: -r[5])

    label_w = max(44, max((len(r[0]) for r in rows), default=0) + 2)
    col = "{:<" + str(label_w) + "}{:>10}{:>10}{:>12}{:>14}{:>14}{:>12}  {}"
    line_w = label_w + 10 + 10 + 12 + 14 + 14 + 12
    print(col.format("agent", "input", "output", "cache_w", "cache_r", "total", "cost($)", "models"))
    print("-" * line_w)
    for key, i, o, cw, cr, total, cost, models in rows:
        print(col.format(key, i, o, cw, cr, total, f"{cost:.4f}", ",".join(models)))
    print("-" * line_w)
    print(col.format("TOTAL", "", "", "", "", grand_tokens, f"{grand_cost:.4f}", ""))

    if missing_cost_sessions:
        print("\n[warning] ccusage 側にコスト情報が見つからなかった session:", missing_cost_sessions)


if __name__ == "__main__":
    main()
